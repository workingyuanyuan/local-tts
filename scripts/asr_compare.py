"""Transcribe audio with ASR and compare against the source text to catch dropped, truncated or extra
speech.

Usage:
  uvx --python 3.12 --with faster-whisper==1.2.1 --with zhconv==1.4.3 \
    python -I scripts/asr_compare.py <source.txt> <audio> [<audio>...] > report.jsonl

Text is normalized (simplified Chinese, lowercase, no punctuation, [..] markers removed, numbers
spelled out). Units are characters for Chinese (CER) and words otherwise (WER).

Every place where the transcript has fewer or more units than the source (a deletion, an insertion,
or a replacement of a different length) is a candidate. Long-form ASR both skips words that are in the
audio and invents text (e.g. after the end of the file), so each candidate is checked again: its
time window (from word timestamps, padded by 1 s, at least 2 s inside the file) is cut out and
transcribed on its own, and aligned against the source around the candidate. The candidate is
resolved only when, in that crop,
- every source unit of the candidate is heard (matched, or something of the same length in its place);
- between the first and last reliable match (>= 3 chars / 2 words) there is no other deletion or
  insertion (a crop cannot fix one gap by introducing another);
- before the first / after the last reliable match there is no extra speech, except a cut word: a
  few units (3 chars / 2 words) within 0.5 s of a crop boundary that cuts through speech (the
  candidate itself is 1 s inside the crop, so it never counts as one).
`unresolved` lists the rest: dropped or extra speech, or ASR noise that only a listener can tell
apart. They are not waived here; a caller may accept them only with a recorded listening check.
Same-length replacements are `substitutions` (misreadings such as 逕流 read as 除流/猪流, or ASR mishearings:
this checks for missing or extra speech, not pronunciation); spans differing only in spacing ("back up"/"backup") count too.
Also reported: `missing_sentences` (< 60% of a sentence's units matched in the first pass),
`deletions` / `insertions` (first-pass runs >= 4 chars / 2 words, >= 8 chars / 4 words), the raw
word timestamps of the first pass and of every crop, the loudest 50 ms inside each candidate's span
(`span_peak_dbfs`, for the listener), input hashes and the ASR settings.
"""
import difflib
import hashlib
import json
import re
import subprocess
import sys

import numpy as np
import zhconv
from faster_whisper import WhisperModel
from faster_whisper.utils import download_model

WHISPER_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
WHISPER_REV = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"

CJK = re.compile(r"[㐀-䶿一-鿿]")


DIGITS = "零一二三四五六七八九"
EN_NUMBERS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
              "sixteen seventeen eighteen nineteen").split()
EN_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def int_to_en(n: int) -> str:
    """0 <= n < 1000 to words, e.g. 48 -> forty eight, 305 -> three hundred five."""
    if n < 20:
        return EN_NUMBERS[n]
    if n < 100:
        return EN_TENS[n // 10] + ("" if n % 10 == 0 else " " + EN_NUMBERS[n % 10])
    return EN_NUMBERS[n // 100] + " hundred" + ("" if n % 100 == 0 else " " + int_to_en(n % 100))


def _section(n: int) -> str:
    """0 < n < 10000 to Chinese numerals with interior zeros, e.g. 1005 -> 一千零五."""
    out, zero = "", False
    for value, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        d = n // value % 10
        if d:
            out += ("零" if zero else "") + DIGITS[d] + unit
            zero = False
        elif out:
            zero = True
    return out


def int_to_zh(n: int) -> str:
    """Integer (< 10^8) to Chinese numerals, e.g. 31 -> 三十一, 48000 -> 四万八千."""
    if n == 0:
        return DIGITS[0]
    high, low = divmod(n, 10000)
    out = _section(high) + "万" if high else ""
    if low:
        out += ("零" if high and low < 1000 else "") + _section(low)
    return out[1:] if out.startswith("一十") else out


def normalize(text: str, cjk: bool) -> str:
    text = re.sub(r"\[[^\]]*\]", " ", text)
    # zh-hans converts characters only (zh-cn would also swap regional words, e.g. 記憶體 -> 内存).
    text = zhconv.convert(text, "zh-hans").lower()
    if cjk:
        # ASR writes numbers as digits; the source spells them out.
        text = re.sub(r"\d+", lambda m: int_to_zh(int(m.group())) if len(m.group()) < 9 else m.group(), text)
    else:  # same for English numbers ("Point 2", "3-2-1", "48kHz"); "by-product" == "byproduct"
        text = re.sub(r"(?<=[a-z])-(?=[a-z])", "", text)
        text = re.sub(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)", " ", text)
        text = re.sub(r"\d+", lambda m: int_to_en(int(m.group())) if len(m.group()) < 4 else m.group(), text)
    return re.sub(r"[^\w\s]|_", " ", text)


def units(text: str, cjk: bool) -> list[str]:
    t = normalize(text, cjk)
    return [c for c in t if not c.isspace()] if cjk else t.split()


def edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


PAD = 1.0  # seconds of audio around a candidate's window
ASR_SETTINGS = {"model": WHISPER_REPO, "revision": WHISPER_REV, "device": "cpu", "compute_type": "int8",
                "beam_size": 5, "temperature": 0.0, "vad_filter": True, "word_timestamps": True,
                "crop": {"pad_seconds": PAD, "condition_on_previous_text": False}}


def transcribe(model, audio: np.ndarray, lang: str, **opts) -> list:
    # VAD keeps Whisper from hallucinating text in long silences (e.g. "请不吝点赞订阅").
    # temperature 0 only: no sampled fallback, so the same audio always gives the same transcript
    segs, _ = model.transcribe(audio, beam_size=5, language=lang, vad_filter=True, word_timestamps=True,
                               temperature=0.0, **opts)
    return [w for s in segs for w in (s.words or [])]


def crop_window(t0: float, t1: float, dur: float) -> tuple[float, float]:
    """[t0, t1] padded by PAD, inside the file and at least 2*PAD long (or the whole file if shorter):
    timestamps past the end of the file (hallucinations) still get the real tail checked."""
    lo, hi = max(0.0, min(t0, t1) - PAD), min(dur, max(t0, t1) + PAD)
    if hi - lo < 2 * PAD:
        lo = max(0.0, min(lo, dur - 2 * PAD))
        hi = min(dur, max(hi, lo + 2 * PAD))
    return lo, hi


def raw_words(words: list, offset: float = 0.0) -> list:
    return [[round(w.start + offset, 2), round(w.end + offset, 2), w.word] for w in words]


CUT_GARBLE_SECONDS = 0.5  # a word cut by the crop boundary lies this close to it


def verify(ref_c: list[str], crop_u: list[str], crop_t: list[tuple[float, float]], crop_len: float, focus: range,
           cut_start: bool, cut_end: bool, edge: int, anchor: int) -> bool:
    """Is the candidate (source units `focus` of `ref_c`) cleanly explained by the crop transcript?
    `crop_t` are the crop units' (start, end) in seconds from the crop start.
    The reliable part of the alignment lies between the first and last match of >= `anchor` units;
    a shorter match at an edge (a garbled cut word matching some far-away unit) is not trusted.
    Extra units outside it are allowed only as a cut word: few, and within CUT_GARBLE_SECONDS of a
    crop boundary that cuts through speech. The candidate itself sits PAD seconds inside the crop,
    so it can never pass as a cut word."""
    ops = difflib.SequenceMatcher(None, ref_c, crop_u, autojunk=False).get_opcodes()
    anchors = [k for k, op in enumerate(ops) if op[0] == "equal" and op[2] - op[1] >= anchor]
    if not anchors:  # nothing of the source reliably heard: fine only for an insertion in silence
        return not focus and not crop_u
    first, last = anchors[0], anchors[-1]
    heard, lead, trail = set(), 0, 0
    for k, (op, a1, a2, b1, b2) in enumerate(ops):
        if op == "equal" or (op == "replace" and (a2 - a1 == b2 - b1 or same_spelling(ref_c[a1:a2], crop_u[b1:b2]))):
            heard.update(range(a1, a2))
            continue
        extra = max(0, (b2 - b1) - (a2 - a1))  # crop units with no source counterpart
        if first < k < last:  # a gap or extra speech inside the reliable part
            return False
        if not extra:
            continue
        if k < first:
            if not (cut_start and all(crop_t[j][0] <= CUT_GARBLE_SECONDS for j in range(b1, b2))):
                return False
            lead += extra
        else:
            if not (cut_end and all(crop_t[j][1] >= crop_len - CUT_GARBLE_SECONDS for j in range(b1, b2))):
                return False
            trail += extra
    if lead > edge or trail > edge:
        return False
    return all(k in heard for k in focus)


def same_spelling(a: list[str], b: list[str]) -> bool:
    """Only the spacing differs ("back up" / "backup")."""
    return "".join(a) == "".join(b)


def timed_units(words: list, cjk: bool) -> tuple[list[str], list[tuple[float, float]]]:
    """Units of the transcript, each with its word's (start, end)."""
    us, ts = [], []
    for w in words:
        for u in units(w.word, cjk):
            us.append(u)
            ts.append((w.start, w.end))
    return us, ts


def main() -> None:
    source = open(sys.argv[1]).read()
    cjk = len(CJK.findall(source)) > len(source) * 0.2
    lang = "zh" if cjk else "en"
    model = WhisperModel(download_model(WHISPER_REPO, revision=WHISPER_REV), device="cpu", compute_type="int8")
    sentences = [x.strip() for x in re.split(r"(?<=[。！？!?；;…])|(?<=[.])\s+|\n+", source) if units(x, cjk)]
    # Sentence units must tile the whole normalized source for the per-sentence coverage to line up.
    assert sum(len(units(x, cjk)) for x in sentences) == len(units(source, cjk)), "sentence split misaligned"
    ref_u = units(source, cjk)
    joiner, del_run, ins_run = ("", 4, 8) if cjk else (" ", 2, 4)
    edge = 3 if cjk else 2  # a crop cut mid-word may garble this many units at a cut edge
    anchor = 3 if cjk else 2  # matches this long anchor the reliable part of a crop alignment
    ctx = 40 if cjk else 15  # source units of context aligned against a crop

    source_sha = hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()

    for path in sys.argv[2:]:
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                             capture_output=True, check=True).stdout
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
        dur = len(audio) / 16000
        words = transcribe(model, audio, lang)
        hyp_u, times = timed_units(words, cjk)
        sm = difflib.SequenceMatcher(None, ref_u, hyp_u, autojunk=False)
        ops = sm.get_opcodes()
        matched = [False] * len(ref_u)
        for block in sm.get_matching_blocks():
            for k in range(block.a, block.a + block.size):
                matched[k] = True
        missing, pos = [], 0
        for sent in sentences:
            n = len(units(sent, cjk))
            if n and sum(matched[pos:pos + n]) / n < 0.6:
                missing.append(sent)
            pos += n

        candidates, substitutions = [], 0
        for op, i1, i2, j1, j2 in ops:
            if op == "equal":
                continue
            if op == "replace" and (i2 - i1 == j2 - j1 or same_spelling(ref_u[i1:i2], hyp_u[j1:j2])):
                substitutions += max(i2 - i1, j2 - j1)  # misheard, or only spaced differently (back up/backup)
                continue
            if j2 > j1:
                t0, t1 = times[j1][0], times[j2 - 1][1]
            else:  # nothing heard: the gap between the neighbouring transcript words
                t0 = times[j1 - 1][1] if j1 > 0 else 0.0
                t1 = times[j1][0] if j1 < len(times) else dur
            lo, hi = crop_window(t0, t1, dur)
            crop_words = transcribe(model, audio[int(lo * 16000):int(hi * 16000)], lang,
                                    condition_on_previous_text=False)
            crop_u, crop_t = timed_units(crop_words, cjk)
            c0 = max(0, i1 - ctx)
            ref_c = ref_u[c0:i2 + ctx]
            resolved = verify(ref_c, crop_u, crop_t, hi - lo, range(i1 - c0, i2 - c0), lo > 0, hi < dur, edge, anchor)
            span = audio[int(min(t0, t1, dur) * 16000):int(min(max(t0, t1), dur) * 16000)]
            peak = max((float(np.sqrt(np.mean(span[k:k + 800] ** 2))) for k in range(0, len(span), 800)), default=0.0)
            candidates.append({"kind": "deletion" if j2 == j1 else "insertion" if i2 == i1 else "replace",
                               # loudest 50 ms in the first-pass span: a hint for the listener (silence there
                               # means the "heard" words were not spoken); not used to resolve anything
                               "span_peak_dbfs": round(20 * np.log10(max(peak, 1e-6)), 1),
                               "source": joiner.join(ref_u[i1:i2]), "heard": joiner.join(hyp_u[j1:j2]),
                               "timestamps": [round(t0, 2), round(t1, 2)], "window": [round(lo, 2), round(hi, 2)],
                               "crop": joiner.join(crop_u), "crop_words": raw_words(crop_words, lo),
                               "resolved": resolved})

        print(json.dumps({
            "file": path, "metric": "cer" if cjk else "wer", "duration": round(dur, 2),
            "audio_sha256": hashlib.sha256(open(path, "rb").read()).hexdigest(), "source_sha256": source_sha,
            "asr": ASR_SETTINGS,
            "error_rate": round(edit_distance(ref_u, hyp_u) / max(1, len(ref_u)), 4),
            "ref_units": len(ref_u), "hyp_units": len(hyp_u), "sentences": len(sentences),
            "unresolved": [c for c in candidates if not c["resolved"]],
            "candidates": len(candidates), "substitutions": substitutions,
            "missing_sentences": missing,
            "deletions": [joiner.join(ref_u[i1:i2]) for op, i1, i2, j1, j2 in ops
                          if op in ("delete", "replace") and (i2 - i1) - (j2 - j1) >= del_run],
            "insertions": [joiner.join(hyp_u[j1:j2]) for op, i1, i2, j1, j2 in ops
                           if op in ("insert", "replace") and (j2 - j1) - (i2 - i1) >= ins_run],
            "transcript": joiner.join(hyp_u), "candidate_details": candidates, "words": raw_words(words),
        }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
