"""Shared fakes: a gated adapter (no GPU) and a fixture running the real app on it."""
import threading
import weakref

import numpy as np
import pytest
from fastapi.testclient import TestClient

from local_tts import config as config_module
from local_tts.adapters import ADAPTERS
from local_tts.adapters.base import Adapter, SegmentAudio
from local_tts.server import app as app_module

GATE = threading.Event()  # generation blocks until set
FAIL_LOAD = {"on": False}
FAIL_GEN = {"on": False}
LOAD_HELD = []  # weakrefs to objects held during a failed load


class Big:
    """Stands in for a partially loaded GPU tensor."""


class GatedAdapter(Adapter):
    languages = {"zh": "Chinese"}
    capabilities = {"clone": True}
    supported_options = {"language", "seed"}

    def __init__(self, entry):
        super().__init__(entry)
        self._loaded = False
        self.partial = None

    @property
    def loaded(self):
        return self._loaded

    def load(self):
        self.partial = Big()
        if FAIL_LOAD["on"]:
            LOAD_HELD.append(weakref.ref(self.partial))
            raise RuntimeError("simulated load failure")
        self._loaded = True

    def unload(self):
        self.partial = None
        self._loaded = False

    def voice_reference(self, voice):
        return None

    def generate(self, text, *, language, reference, max_frames, seed):
        GATE.wait(10)
        if FAIL_GEN["on"]:
            raise RuntimeError("simulated generation failure")
        return SegmentAudio(np.full((2, 1000), 0.3, np.float32), 1000, 12, False)  # 1 s: plausible for short text


def make_client(monkeypatch, tmp_path):
    monkeypatch.setitem(ADAPTERS, "moss_local", "tests.fakes:GatedAdapter")
    (tmp_path / "server.yaml").write_text("queue_max: 2\nidle_unload_seconds: 1\nsync_timeout_seconds: 1\n")
    for name in ("models.yaml", "voices.yaml"):
        (tmp_path / name).write_text((config_module.CONFIG_DIR / name).read_text())
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    GATE.clear()
    FAIL_LOAD["on"] = False
    FAIL_GEN["on"] = False
    with TestClient(app_module.build_app()) as c:
        try:
            yield c
        finally:
            GATE.set()  # release a blocked job before shutdown joins the worker
