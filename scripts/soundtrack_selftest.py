"""Self-test of step4_check's video checks: soundtrack() (a video's audio must be its narration clips)
and scene_cuts() (one scene change between consecutive clips).

Usage: .venv/bin/python scripts/soundtrack_selftest.py outputs/step4/soundtrack_validation
Files: three clips (scene1-3.wav, Codex round 6) and soundtracks built from them (AAC):
  good.m4a          scene1+2+3
  swapped.m4a       scene1 + another clip (Claude's scene02) + scene3
  extra.m4a         scene1+2+3 + another clip (Claude's scene01) appended
  word_dropped.m4a  scene1+2+3 with 0.26 s silenced 1.5 s into scene3 (one word)
  blip_after.m4a    scene1+2+3 + a 25 ms tone at amplitude 0.8
  pause_filled.m4a  scene1+2+3 with 0.4 s of other speech put into a pause inside scene2
  white_titles.mp4  three 7 s white cards whose only difference is a short centred title
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from step4_check import scene_cuts, soundtrack  # noqa: E402

d = Path(sys.argv[1])
clips = [d / f"scene{i}.wav" for i in (1, 2, 3)]
fail = 0
for name, want_ok in [("good", True), ("swapped", False), ("extra", False), ("word_dropped", False),
                      ("blip_after", False), ("pause_filled", False)]:
    problems, info, _ = soundtrack(d / f"{name}.m4a", clips)
    ok = (not problems) == want_ok
    fail += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}: {'accepted' if not problems else 'rejected'} {problems or info}")
cuts = scene_cuts(d / "white_titles.mp4", 21.0)
ok = len(cuts) == 2 and abs(cuts[0] - 7) < 0.2 and abs(cuts[1] - 14) < 0.2
fail += not ok
print(f"{'PASS' if ok else 'FAIL'} white_titles: scene changes {cuts} (want ~7 and ~14 s)")
sys.exit(fail)
