import pytest

from tests.fakes import make_client


@pytest.fixture
def client(monkeypatch, tmp_path):
    yield from make_client(monkeypatch, tmp_path)
