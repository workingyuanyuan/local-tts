"""Self-test of asr_compare's candidate checks on injected transcripts (no audio, no ASR run).

Usage: uvx --python 3.12 --with faster-whisper==1.2.1 --with zhconv==1.4.3 python scripts/asr_compare_selftest.py
Cases from the step-4 reviews: each pair is (source, crop transcript) for one candidate.
"""
import difflib
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from asr_compare import crop_window, units, verify  # noqa: E402

UNIT_S = 0.3  # seconds per unit in the injected crops


def check(source: str, first: str, crop: str, cjk: bool, cut_start=False, cut_end=False) -> bool:
    """Resolve every candidate of `first` (the first-pass transcript) against the same `crop`."""
    ref, hyp = units(source, cjk), units(first, cjk)
    ok = True
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ref, hyp, autojunk=False).get_opcodes():
        if op == "equal" or (op == "replace" and i2 - i1 == j2 - j1):
            continue
        crop_u = units(crop, cjk)
        crop_t = [(j * UNIT_S, (j + 1) * UNIT_S) for j in range(len(crop_u))]  # evenly spoken crop
        ok &= verify(ref, crop_u, crop_t, len(crop_u) * UNIT_S, range(i1, i2), cut_start, cut_end,
                     3 if cjk else 2, 3 if cjk else 2)
    return ok


CASES = [  # (expected resolved, source, first pass, crop, cjk, cut_start, cut_end, what)
    (False, "Do delete the backup.", "Do not delete the backup", "Do not delete the backup", False, False, False,
     "extra 'not' confirmed by the crop"),
    (False, "Do not delete the backup.", "Do delete the backup", "Now delete the backup", False, False, False,
     "'do not' -> 'now': 'not' is not heard"),
    (False, "Do not delete the backup.", "Do delete the backup", "Do not permanently delete the backup", False,
     False, False, "crop recovers 'not' but adds 'permanently'"),
    (False, "請刪除舊的備份。", "請不刪除舊的備份", "請不刪除舊的備份", True, False, False, "extra 不 confirmed"),
    (False, "Keep an external drive.", "Keep external drive", "Keep external drive", False, False, False,
     "article dropped in both passes"),
    (False, "就到這裡。", "就到這裡謝謝收看大家再見", "就到這裡謝謝收看大家再見", True, False, False,
     "run-on speech at the end of the file"),
    (True, "Do not delete the backup.", "Do delete the backup", "Do not delete the backup", False, False, False,
     "crop hears the dropped word"),
    (True, "Keep one backup off site.", "Keep one backup", "um Keep one backup off site", False, True, False,
     "one garbled unit where the crop cuts through speech"),
    (False, "Keep one backup off site.", "Keep one backup", "um Keep one backup off site", False, False, False,
     "the same extra unit at the real file start"),
    (True, "就到這裡。", "就到這裡謝謝收看", "就到這裡", True, False, False, "hallucinated tail gone in the crop"),
    (True, "水沿著地面流動，叫做逕流。第三，水循環讓地球上的淡水不斷更新。",
     "水沿著地面流動叫做逕流第三水循環讓地球上的淡水不斷更新第三水循環的能量", "流上的淡水不斷更新", True, True, False,
     "round 8: the cut word 流 must not anchor to 逕流 far away"),
    (False, "計時器一響，就休息五分鐘。", "記起祥就休息五分鐘", "自己想就休息五分", True, False, True,
     "round 8: 計時器一響 heard as three other syllables in both passes"),
    (False, "Start this backup process now keep one backup off site.",
     "Start this backup process now do not keep one backup off site", "now do not keep one backup off site",
     False, True, False, "round 4 review: extra 'do not' after a fully heard edge word is not a cut word"),
    (False, "現在開始，保留一份異地備份。", "現在開始不要保留一份異地備份", "始不要保留一份異地備份", True, True, False,
     "round 4 review: extra 不要 after the cut word is not a cut word"),
    (True, "Keep one backup off site.", "Keep one back up off site", "Keep one back up off site", False, False, False,
     "spacing only (back up / backup) in the crop too"),
]

fail = 0
for want, source, first, crop, cjk, cs, ce, what in CASES:
    got = check(source, first, crop, cjk, cs, ce)
    fail += got != want
    print(f"{'PASS' if got == want else 'FAIL'} resolved={got} (want {want}): {what}")
lo, hi = crop_window(12.0, 14.0, 10.0)  # candidate entirely past the end of a 10 s file
ok = 0 <= lo < hi <= 10.0 and hi - lo >= 2.0
fail += not ok
print(f"{'PASS' if ok else 'FAIL'} window past the end -> [{lo}, {hi}]")
sys.exit(fail)
