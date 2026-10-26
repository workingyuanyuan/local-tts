"""Pipeline tests with a fake adapter (no GPU): truncation splitting, retries, failure reporting."""
import numpy as np
import pytest
import torch

from local_tts.adapters.base import Adapter, SegmentAudio
from local_tts.config import ModelEntry, SegmentingConfig
from local_tts.engine import SynthesisRequest, synthesize
from local_tts.errors import TTSError
from local_tts.text import estimate_seconds, split_text

SR = 1000


class FakeAdapter(Adapter):
    languages = {"zh": "Chinese"}
    supported_options = {"language", "seed"}

    def __init__(self, behaviour):
        super().__init__(ModelEntry("fake", "fake", "", {"max_new_tokens": 4096}))
        self.behaviour = behaviour  # fn(text, call_index) -> "ok" | "truncate" | "short" | "long" | seconds
        self.calls = []
        self.max_frames_seen = []

    loaded = True

    def load(self): ...

    def unload(self): ...

    def voice_reference(self, voice):
        return torch.full((5, 12), -1)

    def generate(self, text, *, language, reference, max_frames, seed):
        self.calls.append((text, reference))
        self.max_frames_seen.append(max_frames)
        kind = self.behaviour(text, len(self.calls) - 1)
        if isinstance(kind, float):  # an exact duration, independent of the estimate
            seconds, kind = kind, "ok"
        else:
            seconds = estimate_seconds(text) * {"short": 0.05, "long": 1.0}.get(kind, 1.0) + (20 if kind == "long" else 0)
        audio = np.full((2, max(1, int(seconds * SR))), 0.5, dtype=np.float32)
        frames = max_frames if kind == "truncate" else int(seconds * 12.5)
        # Like the real adapter, the reusable reference is a multi-element tensor (codes of this text).
        ref = torch.tensor([ord(c) for c in text[:4]] * 3).reshape(1, -1)
        return SegmentAudio(audio, SR, frames, kind == "truncate", reference=ref)


def ref_text(ref):
    return None if ref is None else "".join(chr(int(c)) for c in ref[0, :4])


def run(adapter, text, **cfg):
    req = SynthesisRequest(model=adapter.entry, voice=None, text=text, language="zh")
    return synthesize(adapter, req, SegmentingConfig(**cfg))


LONG = "第一句話在這裡，內容有一點長，需要好幾秒鐘才能唸完。" * 3


def test_truncation_splits_and_succeeds():
    a = FakeAdapter(lambda text, i: "truncate" if i == 0 else "ok")
    res = run(a, LONG)
    assert res.splits == 1
    assert len(a.calls) >= 3  # 1 truncated + >=2 halves


def test_persistent_truncation_reports_segment():
    a = FakeAdapter(lambda text, i: "truncate")
    with pytest.raises(TTSError) as e:
        run(a, "開頭一句。\n\n" + LONG, max_split_depth=1)
    assert e.value.code == "segment_truncated"
    assert e.value.details["segment"] == 0


def test_short_output_retried_then_fails_with_segment_index():
    a = FakeAdapter(lambda text, i: "short" if "第二" in text else "ok")
    with pytest.raises(TTSError) as e:
        run(a, "第一段的內容很長很長很長很長很長很長很長。\n\n第二段的內容也很長很長很長很長很長很長。", max_retries=2)
    assert e.value.code == "segment_failed"
    assert e.value.details["segment"] == 1
    assert sum("第二" in t for t, _ in a.calls) == 3  # first try + 2 retries


def test_short_output_recovers_on_retry():
    a = FakeAdapter(lambda text, i: "short" if i == 0 else "ok")
    res = run(a, LONG)
    assert res.retries == 1


def test_first_segment_becomes_reference_when_no_voice():
    a = FakeAdapter(lambda text, i: "ok")
    run(a, "第一段的內容很長很長很長很長很長很長很長。\n\n第二段的內容也很長很長很長很長很長很長。")
    assert a.calls[0][1] is None
    assert ref_text(a.calls[1][1]) == "第一段的"


def test_no_reference_chaining_when_disabled():
    a = FakeAdapter(lambda text, i: "ok")
    run(a, "第一段的內容很長很長很長很長很長很長很長。\n\n第二段的內容也很長很長很長很長很長很長。",
        unvoiced_consistency="none")
    assert all(ref is None for _, ref in a.calls)


def test_gaps_and_pause_markers_become_silence():
    a = FakeAdapter(lambda text, i: "ok")
    res = run(a, "[pause 1s]第一句。\n\n第二句。[pause 2s]", segment_gap_seconds=0.3, paragraph_gap_seconds=0.5)
    speech = sum(estimate_seconds(t) for t, _ in a.calls)
    assert abs(res.duration - (speech + 1 + 0.5 + 2)) < 0.05


def test_split_never_breaks_markers():
    text = "這是一個很長的句子[pause 1.5s]後面接著更多更多的內容，" * 20
    for seg in split_text(text, 5):
        assert seg.text.count("[") == seg.text.count("]")


def test_truncation_with_tensor_references_and_chaining():
    # Regression: re-split children must not evaluate tensor truthiness, and without a voice the
    # first good child becomes the reference for the following children and segments.
    a = FakeAdapter(lambda text, i: "truncate" if i == 0 else "ok")
    res = run(a, LONG + "\n\n" + "第二段的內容也很長很長很長很長很長很長。")
    assert res.splits == 1
    first_child = a.calls[1]
    assert first_child[1] is None
    assert all(ref_text(ref) == first_child[0][:4] for _, ref in a.calls[2:])


def test_resplit_keeps_pause_as_silence():
    text = "甲" * 40 + "[pause 5s]" + "乙" * 40
    a = FakeAdapter(lambda t, i: "truncate" if i == 0 else "ok")
    res = run(a, text, segment_gap_seconds=0.3)
    assert all("[pause" not in t for t, _ in a.calls[1:])
    speech = sum(estimate_seconds(t) for t, _ in a.calls[1:])
    gaps = 0.3 * (len(a.calls) - 2)
    assert abs(res.duration - (speech + 5 + gaps)) < 0.05


def test_long_quoted_cjk_can_be_split():
    text = "「" + "這是一段沒有任何標點符號的超長引文內容" * 100 + "」"
    segs = split_text(text, 30)
    assert len(segs) > 1 and max(s.est_seconds for s in segs) <= 30
    assert "".join(s.text for s in segs) == text


def test_long_unspaced_thai_can_be_split():
    text = ("สวัสดีครับวันนี้เราจะมาพูดถึงระบบสังเคราะห์เสียง" * 50
            + "ภาษาไทยไม่มีการเว้นวรรคระหว่างคำจึงต้องตัดตามตัวอักษร" * 40)
    segs = split_text(text, 30)
    assert len(segs) > 1 and max(s.est_seconds for s in segs) <= 30
    assert "".join(s.text for s in segs) == text
    from local_tts.text import split_finer
    assert len(split_finer(text)) >= 2


def test_budget_never_exceeds_configured_cap():
    a = FakeAdapter(lambda text, i: "ok")
    a.entry.options["max_new_tokens"] = 100
    run(a, "短句。")
    assert a.max_frames_seen == [100]


def test_run_on_output_is_retried():
    # Simulated run-on output: 20 s extra on a short closing paragraph.
    a = FakeAdapter(lambda text, i: "long" if i == 1 else "ok")
    res = run(a, LONG + "\n\n就到這裡。")
    assert res.retries == 1 and len(a.calls) == 3
    assert res.audio.shape[1] / SR < estimate_seconds(LONG + "就到這裡。") + 5


def test_persistent_run_on_output_fails_with_segment_index():
    a = FakeAdapter(lambda text, i: "long" if "到這裡" in text else "ok")
    with pytest.raises(TTSError) as e:
        run(a, LONG + "\n\n就到這裡。", max_retries=2)
    assert e.value.code == "segment_failed" and e.value.details["segment"] == 1
    assert "run-on" in e.value.message


def test_digit_strings_are_not_estimated_as_one_word():
    # A 20-digit ID read digit by digit must not look like run-on output (review, step 4 round 2).
    est = estimate_seconds("編號 12345678901234567890。")
    assert est >= 20 * 0.3



def test_mixed_alphanumerics_and_abbreviations_are_estimated_per_symbol():
    assert estimate_seconds("RTX5090") >= 7 * 0.3      # R-T-X five-zero-nine-zero
    assert estimate_seconds("API v1.5") >= 5 * 0.3     # A-P-I v one five


def test_uppercase_sentences_are_not_spelled_out():
    lower = "the quick brown fox jumps over the lazy dog."
    assert estimate_seconds(lower.upper()) == pytest.approx(estimate_seconds(lower))
    assert estimate_seconds("Our API runs on an RTX5090 card.") > estimate_seconds("Our app runs on a big card.")


@pytest.mark.parametrize("text, seconds", [
    # Durations written down from real readings (not from the estimator): step-4 clips of these
    # lengths are normal speech and must be kept as they are.
    ("這是一句正常長度的中文旁白。", 3.8),                 # 13 chars at ~1.5x the estimate (measured zh rate)
    ("這是一句正常長度的中文旁白。", 5.6),                 # same text read slowly
    ("THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG.", 3.0),  # uppercase English at a normal pace
    ("Keep three copies: your original files, plus two backups.", 3.5),
])
def test_normal_readings_are_not_retried(text, seconds):
    a = FakeAdapter(lambda t, i: seconds)
    res = run(a, text)
    assert res.retries == 0 and len(a.calls) == 1


def test_run_on_threshold_boundary():
    from local_tts.engine import MAX_DURATION_RATIO, MAX_DURATION_SLACK
    text = "這是一句正常長度的中文旁白。"
    limit = MAX_DURATION_RATIO * estimate_seconds(text) + MAX_DURATION_SLACK
    for seconds, retried in [(limit - 0.2, False), (limit + 0.5, True)]:
        a = FakeAdapter(lambda t, i, s=seconds: s if i == 0 else "ok")
        assert (run(a, text).retries == 1) is retried, (seconds, limit)


def test_short_first_piece_is_rerendered_with_the_reference_voice():
    # Final audit: a title shorter than MIN_REFERENCE_SECONDS used to keep its own (different) voice.
    a = FakeAdapter(lambda text, i: "ok")
    res = run(a, "標題。\n\n" + LONG, paragraph_gap_seconds=0.5)
    texts = [t for t, _ in a.calls]
    assert texts[0] == texts[-1] == "標題。" and a.calls[0][1] is None
    assert ref_text(a.calls[-1][1]) == LONG[:4]  # the title again, now with the long paragraph's voice
    speech = estimate_seconds("標題。") + estimate_seconds(LONG)
    assert abs(res.duration - (speech + 0.5)) < 0.05


def test_suspiciously_short_segment_is_retried():
    # A dropped sentence: 0.6x the estimate (the hard limit is 0.3x) triggers a retry.
    est = estimate_seconds(LONG)
    a = FakeAdapter(lambda text, i: est * 0.6 if i == 0 else "ok")
    res = run(a, LONG)
    assert res.retries == 1 and not res.warnings and abs(res.duration - est) < 0.05


def test_persistently_short_segment_keeps_longest_attempt_with_warning():
    est = estimate_seconds(LONG)
    lengths = [0.5, 0.65, 0.6]
    a = FakeAdapter(lambda text, i: est * lengths[i])
    res = run(a, LONG)
    assert res.retries == 2 and abs(res.duration - est * 0.65) < 0.05
    assert len(res.warnings) == 1 and "segment 0" in res.warnings[0]


@pytest.mark.parametrize("ratio, retried", [(0.66, True), (0.69, True), (0.70, False)])
def test_suspect_threshold_has_no_slack(ratio, retried):
    est = estimate_seconds(LONG)
    a = FakeAdapter(lambda text, i: est * ratio if i == 0 else "ok")
    assert run(a, LONG).retries == (1 if retried else 0)


def test_rerendered_early_piece_drops_its_old_warning():
    # The short title is also "suspiciously short" on every first take; its warning must go away
    # once it is re-rendered (with the reference) at a normal length.
    title = "這是一個比較長的標題句子。"
    t_est, calls = estimate_seconds(title), []

    def behaviour(text, i):
        calls.append(text)
        first_takes = calls.count(title) <= 3
        return t_est * 0.5 if text == title and first_takes else "ok"
    res = run(FakeAdapter(behaviour), title + "\n\n" + LONG)
    assert calls.count(title) == 4 and res.warnings == []
