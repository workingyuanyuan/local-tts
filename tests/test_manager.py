"""ModelManager: one model at a time, no double loads, unload before switching."""
from local_tts.adapters import ADAPTERS
from local_tts.config import ModelEntry, Registry
from local_tts.engine import ModelManager

EVENTS = []


class Recorder:
    languages, capabilities, supported_options = {}, {}, set()

    def __init__(self, entry):
        self.entry = entry
        self.loaded = False

    def load(self):
        EVENTS.append(("load", self.entry.id))
        self.loaded = True

    def unload(self):
        EVENTS.append(("unload", self.entry.id))
        self.loaded = False


def test_switch_unloads_first(monkeypatch):
    monkeypatch.setitem(ADAPTERS, "rec", "tests.test_manager:Recorder")
    reg = Registry("a", {m: ModelEntry(m, "rec", "", {}) for m in "ab"}, {})
    mgr = ModelManager(reg)
    EVENTS.clear()
    mgr.acquire("a")
    mgr.acquire("a")  # already loaded: no reload
    mgr.acquire("b")
    assert EVENTS == [("load", "a"), ("unload", "a"), ("load", "b")]
    assert mgr.loaded_model == "b"
    assert mgr.unload() == "b" and mgr.loaded_model is None
