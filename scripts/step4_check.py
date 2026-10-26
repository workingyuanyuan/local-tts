"""Assertions for scripts/step4_run.sh. Every line printed is "PASS|FAIL <message>" (or "INFO ...").

  step4_check.py task <task> <dir> <evidence.json> <exit code> <seconds> <start epoch> <end epoch> <out_dir>
      Checks one agent run; appends "<script>\\t<audio>" ASR pairs for each completed clip to
      <out_dir>/asr_pairs.tsv, and the final video against all its clips' scripts (file-name order) to
      <out_dir>/asr_info.tsv (reference only: the video's soundtrack is compared with the clips
      sample by sample instead).
  step4_check.py asr-info <asr_info.jsonl>
      Reference lines for the whole-video ASR.
  step4_check.py asr <asr.jsonl> <asr_pairs.tsv> [asr_review.tsv]
      One result per pair; PASS when asr_compare left no candidate unresolved (no dropped or extra
      speech after re-transcribing each mismatch on its own), or each remaining one is in the
      listening-check file with verdict ok.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

VIDEOS = {"claude_video": "intro.mp4", "codex_video": "explainer.mp4"}
VIDEO_SECONDS = (19.5, 30.5)  # the prompts ask for 20-30 s
AUDIO_EXT = {".wav", ".mp3", ".flac"}
DEFAULT_OUTPUT = Path.home() / ".local/share/local-tts/output"


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True)
    p = json.loads(out.stdout or "{}")
    dur = lambda kind: max([float(s.get("duration", 0)) for s in p.get("streams", [])
                            if s.get("codec_type") == kind] or [0.0])
    kinds = {s.get("codec_type") for s in p.get("streams", [])}
    return dict(video="video" in kinds, audio="audio" in kinds, video_s=dur("video"), audio_s=dur("audio"))


def decodes(path: Path) -> bool:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    return r.returncode == 0 and not r.stderr.strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


SR = 8000  # soundtrack comparison rate
WIN = 800  # 100 ms windows
SPEECH_DBFS = -40.0  # a clip window louder than this is speech that must be in the soundtrack
# Per speech window, the soundtrack must carry the clip itself. Normal videos (AAC once or twice,
# 29 clips of rounds 5-8) measured correlation >= 0.63 and energy within +-0.3 dB in every window;
# a silenced word drops the energy by > 20 dB, other speech has correlation near 0.
MIN_WINDOW_CORR = 0.4
MAX_WINDOW_ENERGY_DB = 3.0
SILENT_DBFS = -40.0


def pcm(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def _db(x: float) -> float:
    return 10 * np.log10(max(x, 1e-12))


def soundtrack(mp4: Path, clips: list[Path]) -> tuple[list[str], list[str], list[tuple[Path, float, float]]]:
    """Is the video's audio exactly the clips, without overlap, with only silence between them?
    Each clip is located by cross-correlation over the whole soundtrack; their positions give the
    video's order. Every 100 ms speech window of a clip must be present (correlation, energy), the
    clip's pauses must stay silent, and everything outside the clips must be silent, down to the
    last sample.
    Returns (problems, info, [(clip, start s, end s)] in video order)."""
    mix = pcm(mp4)
    problems, info, placed = [], [], []
    for p in clips:
        c = pcm(p)
        if len(c) > len(mix):
            problems.append(f"{p.name} is longer than the soundtrack")
            continue
        n = 1 << int(np.ceil(np.log2(len(mix) + len(c))))
        corr = np.fft.irfft(np.fft.rfft(mix, n) * np.conj(np.fft.rfft(c, n)), n)[:len(mix) - len(c) + 1]
        off = int(np.argmax(corr))
        part = mix[off:off + len(c)]
        bad = []
        for k in range(0, len(c), WIN):
            cw, pw = c[k:k + WIN], part[k:k + WIN]
            e = float(np.mean(cw ** 2))
            if _db(e) <= SPEECH_DBFS:  # a pause in the clip must stay a pause in the video
                if _db(float(np.mean(pw ** 2))) > SILENT_DBFS:
                    bad.append(k / SR)
                continue
            rho = float(np.dot(cw, pw) / (np.linalg.norm(cw) * np.linalg.norm(pw) + 1e-12))
            ratio = _db(float(np.mean(pw ** 2))) - _db(e)
            if rho < MIN_WINDOW_CORR or abs(ratio) > MAX_WINDOW_ENERGY_DB:
                bad.append(k / SR)
        snr = _db(float(np.sum(c ** 2))) - _db(float(np.sum((part - c * float(np.dot(part, c) / max(float(np.dot(c, c)), 1e-12))) ** 2)))
        info.append(f"{p.name}@{off / SR:.2f}s snr={snr:.1f}dB")
        if bad:
            problems.append(f"{p.name}: the soundtrack differs from the clip at {len(bad)} speech windows "
                            f"(first at {bad[0]:.1f}s into the clip)")
        placed.append((p, off / SR, (off + len(c)) / SR))
    placed.sort(key=lambda x: x[1])
    covered = np.zeros(len(mix), bool)
    for (p, a, b), nxt in zip(placed, placed[1:] + [None]):
        if nxt is not None and nxt[1] < b - 0.05:
            problems.append(f"{p.name} and {nxt[0].name} overlap in the soundtrack")
        covered[int(a * SR):int(b * SR)] = True
    # every uncovered stretch, cut into 50 ms windows including the last partial one
    loud, idx = -120.0, np.flatnonzero(~covered)
    if len(idx):
        runs = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
        for run in runs:
            seg = mix[run[0]:run[-1] + 1]
            for k in range(0, len(seg), WIN // 2):
                loud = max(loud, _db(float(np.mean(seg[k:k + WIN // 2] ** 2))))
    info.append(f"outside clips {int((~covered).sum()) / SR:.2f}s loudest 50 ms {loud:.0f}dBFS")
    if loud > SILENT_DBFS:
        problems.append(f"sound outside the narration clips ({loud:.0f} dBFS)")
    return problems, info, placed


def scene_cuts(path: Path, duration: float) -> list[float]:
    """Times where the picture changes. Static title cards score ~1e-5 between frames, >= 0.03 at a cut
    (similar colours); white cards whose short centred title changes score ~0.0098. Detections within
    1 s are one change (fade out, cut, fade in); changes in the first and last 0.5 s are the video
    fading in or out, not scene changes."""
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-an", "-vf", "select='gt(scene,0.002)',showinfo",
                        "-f", "null", "-"], capture_output=True, text=True)
    cuts = []
    for t in (float(x) for x in re.findall(r"pts_time:([\d.]+)", r.stderr)):
        if not cuts or t - cuts[-1] > 1.0:
            cuts.append(t)
    return [t for t in cuts if 0.5 < t < duration - 0.5]


def service_log(start: float, end: float) -> str:
    """The service's journal for [start, end]; raises if it cannot be read."""
    return subprocess.run(["journalctl", "--user", "-u", "local-tts", "--since", f"@{int(start)}",
                           "--until", f"@{int(end) + 1}", "--no-pager", "-o", "short-iso"],
                          capture_output=True, text=True, check=True).stdout


def new_audio(root: Path, since: float, until: float) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*") if p.suffix.lower() in AUDIO_EXT and p.is_file()
                  and since <= p.stat().st_mtime <= until + 1)


def task(name, d, evidence, rc, secs, start, end, out_dir) -> list[str]:
    d, out_dir, start, end = Path(d).resolve(), Path(out_dir), float(start), float(end)
    ev = json.load(open(evidence, encoding="utf-8"))
    for key in ("calls", "speech_completed", "files", "scripts", "tool_errors", "shell_tts", "unparsed_results"):
        if key not in ev:
            return [f"FAIL {name}: evidence has no '{key}' (transcript not parsed)"]
    gen = ev["calls"].get("generate_speech", 0)
    lines = []

    if name.endswith("_negative"):
        # Negative tasks run alone (step4_all.sh), so any job the service queued meanwhile came from them.
        # The first check saves what it saw (service log of the task's window, new audio files); a
        # recheck uses that record instead of today's directories and journal.
        saved = out_dir / f"{name}.negative.json"
        if saved.is_file():
            seen = json.load(open(saved, encoding="utf-8"))
        else:
            seen = {"window": [start, end], "service_log": service_log(start, end),
                    "new_audio": [str(p) for p in new_audio(d, start, end) + new_audio(DEFAULT_OUTPUT, start, end)]}
            json.dump(seen, open(saved, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        stray = seen["new_audio"]
        jobs = len(re.findall(r"job \w+ queued", seen["service_log"]))
        ok = (rc == "0" and not ev["calls"] and not ev["shell_tts"] and not stray and jobs == 0
              and (d / "script.md").is_file() and (d / "script.md").stat().st_size > 0)
        lines.append(f"{'PASS' if ok else 'FAIL'} {name} ({secs}s, local-tts calls {ev['calls'] or 0}, "
                     f"shell TTS {len(ev['shell_tts'])}, service jobs {jobs}, new audio {stray}, "
                     f"script.md {(d / 'script.md').is_file()})")
        return lines

    files = [Path(p) for p in ev["files"]]
    problems = []
    if rc != "0":
        problems.append(f"exit {rc}")
    if ev["unparsed_results"]:
        problems.append(f"{ev['unparsed_results']} speech results could not be read")
    if gen < 1 or not files:
        problems.append("no completed generate_speech")
    for p in files:
        rp = p.resolve()
        if not str(rp).startswith(str(d) + os.sep):
            problems.append(f"{p} is outside the task directory")
        elif not rp.is_file() or not decodes(rp):
            problems.append(f"{p} missing or does not decode")
        elif str(p) not in ev["scripts"]:
            problems.append(f"{p} has no source text for ASR")
        else:
            copy = out_dir / name / rp.relative_to(d)
            if not copy.is_file() or sha256(copy) != sha256(rp):
                problems.append(f"copy of {p} differs")
    detail = (f"{secs}s, generate_speech {gen}, completed {ev['speech_completed']}, files {len(files)}, "
              f"tool errors {ev['tool_errors']}")

    # ASR runs on the hash-checked copies under out_dir, which outlive the work directory
    pairs = [(ev["scripts"][str(p)], (out_dir / name / p.resolve().relative_to(d)).resolve()) for p in files
             if str(p) in ev["scripts"] and str(p.resolve()).startswith(str(d) + os.sep)]
    info_pairs = []
    if name in VIDEOS:
        mp4 = d / VIDEOS[name]
        m = probe(mp4) if mp4.is_file() else dict(video=False, audio=False, video_s=0, audio_s=0)
        clips = sorted({p.resolve() for p in files if p.resolve().is_file()})
        clip_s = sum(probe(p)["audio_s"] for p in clips)
        lo, hi = VIDEO_SECONDS
        if not (m["video"] and m["audio"]):
            problems.append(f"{mp4.name} lacks a video or audio stream")
        if not lo <= m["audio_s"] <= hi:
            problems.append(f"{mp4.name} audio {m['audio_s']:.2f}s outside {lo}-{hi}s")
        if m["video_s"] < m["audio_s"] - 0.5:
            problems.append(f"{mp4.name} picture ends before the speech")
        if len(clips) < 3:
            problems.append(f"{len(clips)} narration clips for 3 scenes")
        if m["audio_s"] < clip_s - 0.5:
            problems.append(f"{mp4.name} audio {m['audio_s']:.2f}s shorter than its clips ({clip_s:.2f}s)")
        # The soundtrack must be the clips themselves (each one is checked by ASR on its own), and
        # there is one scene per clip: each scene change falls between one clip's speech and the
        # next one's. The clips' positions in the soundtrack give the video's order.
        track = soundtrack(mp4, clips) if mp4.is_file() and m["audio"] else (["no soundtrack"], [], [])
        problems += track[0]
        placed = track[2]
        cuts = scene_cuts(mp4, m["video_s"]) if mp4.is_file() else []
        if len(cuts) != len(clips) - 1:
            problems.append(f"{len(cuts)} scene changes at {[round(c, 2) for c in cuts]} for {len(clips)} clips")
        elif len(placed) == len(clips):
            for k, cut in enumerate(cuts):
                end, nxt = placed[k][2], placed[k + 1][1]
                if not end - 0.5 <= cut <= nxt + 0.5:
                    problems.append(f"scene change at {cut:.2f}s, but {placed[k][0].name} speaks until {end:.2f}s "
                                    f"and {placed[k + 1][0].name} starts at {nxt:.2f}s")
        detail += (f", {mp4.name} video {m['video_s']:.2f}s audio {m['audio_s']:.2f}s, "
                   f"{len(clips)} clips {clip_s:.2f}s, scene changes {[round(c, 2) for c in cuts]}, "
                   f"clips in video {[(p.name, round(a, 2), round(b, 2)) for p, a, b in placed]}, "
                   f"soundtrack {track[1]}")
        if mp4.is_file():  # for reference only: ASR of the whole video against all clip texts, in video order
            by_path = {Path(k).resolve(): v for k, v in ev["scripts"].items()}
            text = "\n".join(by_path[p] for p, _, _ in placed if p in by_path)
            copy = (out_dir / name / mp4.relative_to(d)).resolve()  # the durable copy, like the clips
            if copy.is_file() and sha256(copy) == sha256(mp4):
                info_pairs.append((text, copy))
            else:
                problems.append(f"copy of {mp4.name} differs")
    lines.append(f"{'PASS' if not problems else 'FAIL'} {name} ({detail}){'' if not problems else ': ' + '; '.join(problems)}")

    scripts_dir = out_dir / f"{name}.scripts"
    scripts_dir.mkdir(exist_ok=True)
    for listing, items in (("asr_pairs.tsv", pairs), ("asr_info.tsv", info_pairs)):
        with open(out_dir / listing, "a", encoding="utf-8") as f:
            for text, audio in items:
                src = scripts_dir / (audio.name + ".txt")
                src.write_text(text, encoding="utf-8")
                f.write(f"{src}\t{audio}\n")
    return lines


def load_review(path) -> dict:
    """Listening checks: "audio sha256<TAB>kind<TAB>source<TAB>heard<TAB>verdict<TAB>who listened, what was
    heard" lines -> {key: (verdict, note)}. Only verdict "ok" with a note accepts a candidate; any other
    verdict (e.g. "defect") is shown but keeps it failing. The audio hash ties it to the exact file."""
    review = {}
    if path and Path(path).is_file():
        for line in open(path, encoding="utf-8"):
            f = line.rstrip("\n").split("\t")
            if len(f) >= 6 and not f[0].startswith("#") and f[5].strip():
                review[tuple(f[:4])] = (f[4], f[5])
    return review


def asr(results, pairs, review_path=None) -> list[str]:
    expected = [line.rstrip("\n").split("\t") for line in open(pairs, encoding="utf-8") if line.strip()]
    review = load_review(review_path)
    got = {}
    for line in open(results, encoding="utf-8"):
        r = json.loads(line)
        got[r["file"]] = r
    lines = []
    for src, audio in expected:
        r = got.get(audio)
        name = "/".join(audio.rsplit("/", 3)[-3:])
        if r is None:
            lines.append(f"FAIL asr {name}: no ASR result")
            continue
        if not (Path(audio).is_file() and Path(src).is_file() and sha256(Path(audio)) == r["audio_sha256"]
                and sha256(Path(src)) == r["source_sha256"]):
            lines.append(f"FAIL asr {name}: the audio or its text changed since the ASR run (or is missing); rerun it")
            continue
        keys = [(r["audio_sha256"], c["kind"], c["source"], c["heard"]) for c in r["unresolved"]]
        ok = {k for k in keys if review.get(k, ("",))[0] == "ok"}
        bad = [k[1:] for k in keys if k not in ok]
        notes = [f"{k[1:]} {review[k][0]}: {review[k][1]}" for k in keys if k in review]
        lines.append(f"{'PASS' if not bad else 'FAIL'} asr {name} error_rate={r['error_rate']} "
                     f"candidates={r['candidates']} unresolved={bad}" + (f" listened={notes}" if notes else ""))
    return lines


def asr_info(results) -> list[str]:
    """Reference lines (not assertions) for the whole-video ASR."""
    return [f"INFO asr {'/'.join(r['file'].rsplit('/', 3)[-3:])} error_rate={r['error_rate']} "
            f"unresolved={[(c['kind'], c['source'], c['heard']) for c in r['unresolved']]}"
            for r in map(json.loads, open(results, encoding="utf-8"))]


if __name__ == "__main__":
    try:
        out = (task(*sys.argv[2:10]) if sys.argv[1] == "task" else asr(*sys.argv[2:5]) if sys.argv[1] == "asr"
               else asr_info(sys.argv[2]))
    except Exception as e:  # noqa: BLE001 - a checker crash is a failure, never a silent pass
        out = [f"FAIL checker {sys.argv[1:3]}: {type(e).__name__}: {e}"]
    print("\n".join(out))
