"""Split long text into synthesis segments at sentence boundaries.

- `[...]` markers (e.g. `[pause 3.2s]`) are atomic and never split.
- Sentences are packed greedily into segments up to `max_seconds` of estimated speech.
- A `[pause Xs]` marker at a segment edge is removed from the text and returned as explicit
  silence, so edge-silence trimming cannot swallow it.
"""
import re
from dataclasses import dataclass

MARKER_RE = re.compile(r"\[[^\[\]\n]*\]")
PAUSE_RE = re.compile(r"\[pause\s+(\d+(?:\.\d+)?)\s*s\]", re.IGNORECASE)
EDGE_PAUSES_RE = re.compile(r"^((?:\s*\[pause\s+\d+(?:\.\d+)?\s*s\])*)(.*?)((?:\[pause\s+\d+(?:\.\d+)?\s*s\]\s*)*)$",
                            re.IGNORECASE | re.DOTALL)
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

SENTENCE_END = set("。！？!?；;…")
CLAUSE_END = set("，,、：:—")
CLOSERS = set("」』”’\"')）】》〉")

# Rough speaking rates, calibrated on step-1 outputs (zh ≈ 0.19 s/char, en ≈ 0.33 s/word).
SECONDS_PER_CJK = 0.2
SECONDS_PER_WORD = 0.34


def pause_values(text: str) -> list[float]:
    """Seconds of every [pause Xs] marker (may include inf for absurd digit strings)."""
    return [float(x) for x in PAUSE_RE.findall(text)]


def estimate_seconds(text: str) -> float:
    pauses = sum(float(x) for x in PAUSE_RE.findall(text))
    plain = MARKER_RE.sub(" ", text)
    cjk = len(CJK_RE.findall(plain))
    words = 0.0
    latin = re.findall(r"[A-Za-z]", plain)
    # In mostly-uppercase text ("THE QUICK BROWN FOX") capitals are ordinary words, not abbreviations.
    shouting = len(latin) >= 10 and sum(c.isupper() for c in latin) > 0.6 * len(latin)
    for w in WORD_RE.findall(CJK_RE.sub(" ", plain)):
        for part in re.findall(r"\d+|\D+", w):  # "RTX5090" -> "RTX", "5090"
            if part.isdigit():  # read digit by digit or as several words ("2026", "48000", IDs)
                words += len(part)
            elif part.isascii() and part.isupper() and len(part) <= 6 and not shouting:  # spelled: "API", "RTX"
                words += len(part)
            else:  # scripts without spaces (e.g. Thai) show up as one long "word"
                words += max(1.0, len(part) / 6) if len(part) > 20 else 1.0
    return pauses + cjk * SECONDS_PER_CJK + words * SECONDS_PER_WORD


@dataclass
class Segment:
    text: str
    est_seconds: float
    lead_silence: float = 0.0   # from [pause] markers at the segment start
    trail_silence: float = 0.0  # from [pause] markers at the segment end
    paragraph_end: bool = False


def _tokens(text: str):
    """Yield (piece, is_marker) with markers kept whole."""
    pos = 0
    for m in MARKER_RE.finditer(text):
        if m.start() > pos:
            yield text[pos:m.start()], False
        yield m.group(0), True
        pos = m.end()
    if pos < len(text):
        yield text[pos:], False


def _split_at(text: str, enders: set[str], period: bool = False) -> list[str]:
    """Split after any char in `enders` (plus trailing closers); markers stay intact.
    With `period`, '.' also ends a sentence when followed by whitespace or the end (so "3.2" and
    "e.g" inside words are kept)."""
    parts, buf = [], ""
    for piece, is_marker in _tokens(text):
        if is_marker:
            buf += piece
            continue
        i = 0
        while i < len(piece):
            ch = piece[i]
            buf += ch
            end = ch in enders
            if period and ch == ".":
                nxt = piece[i + 1] if i + 1 < len(piece) else ""
                end = nxt == "" or nxt.isspace()
            if end:
                while i + 1 < len(piece) and piece[i + 1] in CLOSERS:
                    i += 1
                    buf += piece[i]
                parts.append(buf)
                buf = ""
            i += 1
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _join(a: str, b: str) -> str:
    if not a:
        return b
    # No space between CJK text (including after full-width punctuation).
    if CJK_RE.match(b[:1]) or CJK_RE.match(a[-1:]) or (a[-1:] in "。！？；…，、：」』”）】》" and CJK_RE.search(a)):
        return a + b
    return a + " " + b


def _hard_split(text: str, max_seconds: float) -> list[str]:
    """Last resort: cut by words (or single CJK characters) without breaking markers."""
    units = []
    for piece, is_marker in _tokens(text):
        if is_marker:
            units.append(piece)
        else:
            # One CJK char, or a run of non-CJK non-space chars (so 「 does not swallow the CJK after it).
            units.extend(re.findall(rf"{CJK_RE.pattern}|(?:(?!{CJK_RE.pattern})\S)+\s*|\s+", piece))
    # A single unit can still be too long (e.g. Thai, which has no spaces): cut it by characters,
    # re-estimating every chunk so none exceeds the limit.
    fine = []
    for u in units:
        while estimate_seconds(u) > max_seconds and not MARKER_RE.fullmatch(u.strip()):
            n = max(1, int(len(u) * max_seconds / estimate_seconds(u)))
            while n > 1 and estimate_seconds(u[:n]) > max_seconds:
                n = max(1, int(n * 0.95))
            fine.append(u[:n])
            u = u[n:]
        if u:
            fine.append(u)
    out, buf = [], ""
    for u in fine:
        if buf and estimate_seconds(buf + u) > max_seconds:
            out.append(buf.strip())
            buf = ""
        buf += u
    if buf.strip():
        out.append(buf.strip())
    return out


def _sentences(paragraph: str, max_seconds: float) -> list[str]:
    out = []
    for line in paragraph.split("\n"):
        for sent in _split_at(line, SENTENCE_END, period=True):
            if estimate_seconds(sent) <= max_seconds:
                out.append(sent)
                continue
            for clause in _pack(_split_at(sent, CLAUSE_END), max_seconds):
                out.extend([clause] if estimate_seconds(clause) <= max_seconds
                           else _hard_split(clause, max_seconds))
    return out


def _pack(pieces: list[str], max_seconds: float) -> list[str]:
    out, buf = [], ""
    for p in pieces:
        cand = _join(buf, p)
        if buf and estimate_seconds(cand) > max_seconds:
            out.append(buf)
            buf = p
        else:
            buf = cand
    if buf:
        out.append(buf)
    return out


def _make_segment(text: str, paragraph_end: bool = False) -> Segment:
    m = EDGE_PAUSES_RE.match(text)
    lead = sum(float(x) for x in PAUSE_RE.findall(m.group(1)))
    trail = sum(float(x) for x in PAUSE_RE.findall(m.group(3)))
    core = m.group(2).strip()
    return Segment(text=core, est_seconds=estimate_seconds(core), lead_silence=lead,
                   trail_silence=trail, paragraph_end=paragraph_end)


def _fold_pause_only(segments: list[Segment]) -> list[Segment]:
    """A segment that is only [pause] markers carries no speech: fold its silence into a neighbour."""
    out: list[Segment] = []
    carry = 0.0
    for seg in segments:
        silence = seg.lead_silence + seg.trail_silence
        if seg.text:
            seg.lead_silence += carry
            carry = 0.0
            out.append(seg)
        elif out:
            out[-1].trail_silence += silence
            out[-1].paragraph_end = out[-1].paragraph_end or seg.paragraph_end
        else:
            carry += silence
    if carry and out:
        out[-1].trail_silence += carry
    return out


def split_text(text: str, max_seconds: float) -> list[Segment]:
    text = text.replace("\r\n", "\n").strip()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    segments: list[Segment] = []
    for p in paragraphs:
        packed = _pack(_sentences(p, max_seconds), max_seconds)
        for i, chunk in enumerate(packed):
            segments.append(_make_segment(chunk, paragraph_end=i == len(packed) - 1))
    return _fold_pause_only(segments)


def split_finer(text: str) -> list[Segment]:
    """Split one segment into ~two halves for truncation retries. Edge [pause] markers of the
    halves become silence, as for normal segments. Returns < 2 segments if it cannot split."""
    target = max(estimate_seconds(text) / 2, 1.0)
    parts: list[str] = []
    for enders, period in ((SENTENCE_END, True), (CLAUSE_END, False)):
        parts = _split_at(text, enders, period)
        if len(parts) > 1:
            parts = _pack(parts, target * 1.2)
            break
    if len(parts) < 2:
        parts = _hard_split(text, target)
    return _fold_pause_only([_make_segment(p) for p in parts])
