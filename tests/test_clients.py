"""Client library, CLI and MCP server against the real app with a fake adapter (no GPU)."""
import asyncio
import json
import os
import shutil
import time

import httpx
import mcp
import pytest

from local_tts import cli, client as client_module, mcp_server
from local_tts.client import ClientError, TTSClient, resolve_output_path, write_file
from tests.fakes import FAIL_GEN, GATE


@pytest.fixture
def tts(client, monkeypatch, tmp_path):
    """TTSClient talking to the in-process app; default output dir under tmp_path."""
    # The TestClient runs the app's lifespan (scheduler); requests go through an async ASGI transport.
    c = TTSClient("http://testserver", transport=httpx.ASGITransport(app=client.app))
    monkeypatch.setattr(client_module, "OUTPUT_DIR", tmp_path / "default_out")
    monkeypatch.setattr(mcp_server, "client", c)
    monkeypatch.setattr(cli, "TTSClient", lambda url: c)
    mcp_server.JOBS.clear()
    return c


# --- paths -------------------------------------------------------------------------------------

def test_resolve_output_path(tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, "OUTPUT_DIR", tmp_path)
    p, fmt = resolve_output_path(None, None)
    assert p.parent == tmp_path and p.suffix == ".wav" and fmt == "wav"
    assert resolve_output_path("/a/b.MP3", None) == (resolve_output_path("/a/b.MP3", None)[0], "mp3")
    assert resolve_output_path("/a/b", "flac")[0].name == "b.flac"
    for bad, code in [("rel/out.wav", "invalid_path"), ("/a/b.ogg", "unsupported_format")]:
        with pytest.raises(ClientError) as e:
            resolve_output_path(bad, None)
        assert e.value.code == code
    with pytest.raises(ClientError) as e:
        resolve_output_path("/a/b.wav", "mp3")
    assert e.value.code == "format_mismatch"


@pytest.mark.skipif(shutil.which("wslpath") is None, reason="WSL only")
def test_windows_path_under_wsl():
    win, _ = resolve_output_path(r"C:\Users\me\narration.wav", None)
    assert str(win) == "/mnt/c/Users/me/narration.wav"


def test_windows_path_without_wsl_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ClientError) as e:
        resolve_output_path(r"C:\Users\me\narration.wav", None)
    assert e.value.code == "invalid_path" and "only supported under WSL" in e.value.message


def test_write_file_never_clobbers_without_overwrite(tmp_path):
    target = tmp_path / "sub" / "x.wav"
    write_file(target, b"one", overwrite=False)
    with pytest.raises(ClientError) as e:
        write_file(target, b"two", overwrite=False)
    assert e.value.code == "output_exists" and target.read_bytes() == b"one"
    write_file(target, b"three", overwrite=True)
    assert target.read_bytes() == b"three"
    assert [p.name for p in target.parent.iterdir()] == ["x.wav"]  # no leftover temp files
    umask = os.umask(0)
    os.umask(umask)
    assert target.stat().st_mode & 0o777 == 0o666 & ~umask


# --- CLI ---------------------------------------------------------------------------------------

def test_cli_say_writes_file_and_reports(tts, tmp_path, capsys):
    GATE.set()
    out = tmp_path / "cli" / "hello.mp3"
    assert cli.main(["say", "你好。", "-o", str(out), "-l", "zh", "--json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["path"] == str(out) and r["format"] == "mp3" and out.exists()
    assert cli.main(["say", "你好。", "-o", str(out)]) == 1  # exists, no --overwrite
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "output_exists"
    assert cli.main(["say", "你好。", "-o", str(out), "--overwrite", "-q"]) == 0


def test_cli_errors_are_structured(tts, capsys):
    assert cli.main(["say", "hi", "--voice", "nope"]) == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["code"] == "unknown_voice" and "demo_zh_female" in err["available"]


def test_cli_no_wait_then_job_fetch(tts, tmp_path, capsys):
    out = tmp_path / "later.wav"
    assert cli.main(["say", "稍後取回。", "-o", str(out), "--no-wait", "--json"]) == 3
    pend = json.loads(capsys.readouterr().out)
    assert pend["status"] == "pending" and pend["output_path"] == str(out)
    GATE.set()
    assert cli.main(["job", pend["job_id"], "--wait", "-o", str(out), "-q"]) == 0
    assert out.exists()


# --- MCP ---------------------------------------------------------------------------------------

def call(name, **args):
    async def go():
        async with mcp.Client(mcp_server.server) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go())


def error_of(result):
    """The SDK prefixes "Error executing tool X: "; the rest is our JSON error."""
    text = result.content[0].text
    return json.loads(text[text.index("{"):])["error"]


def test_mcp_tools_listed():
    async def go():
        async with mcp.Client(mcp_server.server) as c:
            return [t.name for t in (await c.list_tools()).tools]
    assert set(asyncio.run(go())) == {"generate_speech", "get_speech_job", "list_models", "list_voices", "tts_status"}


def test_mcp_generate_completed(tts, tmp_path):
    GATE.set()
    out = tmp_path / "mcp" / "n.wav"
    r = call("generate_speech", text="旁白測試。", output_path=str(out), language="zh")
    assert not r.is_error, r.content
    res = r.structured_content
    assert res["status"] == "completed" and res["path"] == str(out) and out.exists()
    assert res["sample_rate"] == 1000 and res["channels"] == 2 and res["voice"] is None


def test_mcp_pending_then_get_job_is_idempotent(tts, tmp_path):
    out = tmp_path / "long.wav"
    r = call("generate_speech", text="很長的旁白。", output_path=str(out), wait_seconds=0.3).structured_content
    assert r["status"] == "pending" and r["estimated_remaining_seconds"] >= 0
    again = call("get_speech_job", job_id=r["job_id"], wait_seconds=0.2).structured_content
    assert again["status"] == "pending" and not out.exists()
    GATE.set()
    done = call("get_speech_job", job_id=r["job_id"]).structured_content
    assert done["status"] == "completed" and out.exists()
    # A repeated call returns the same result instead of failing on the now-existing file.
    assert call("get_speech_job", job_id=r["job_id"]).structured_content == done


def test_mcp_errors_are_structured(tts, tmp_path):
    out = tmp_path / "exists.wav"
    out.write_bytes(b"x")
    cases = [({"text": "hi", "output_path": str(out)}, "output_exists"),
             ({"text": "hi", "output_path": "relative.wav"}, "invalid_path"),
             ({"text": "hi", "model": "tts-1"}, "unknown_model"),
             ({"text": "hi", "language": "klingon"}, "unsupported_language")]
    for args, code in cases:
        r = call("generate_speech", **args)
        assert r.is_error
        assert error_of(r)["code"] == code, r.content[0].text
    assert out.read_bytes() == b"x"


def test_mcp_lists(tts):
    models = call("list_models").structured_content["result"]
    assert models[0]["id"] == "moss-tts-local-v1.5" and models[0]["default"] is True
    voices = call("list_voices", model="moss-tts-local-v1.5").structured_content["result"]
    assert {v["name"] for v in voices} == {"demo_zh_female", "demo_en"}
    assert call("tts_status").structured_content["status"] == "ok"


def test_server_unreachable_is_reported(monkeypatch):
    c = TTSClient("http://127.0.0.1:9")
    monkeypatch.setattr(mcp_server, "client", c)
    r = call("tts_status")
    assert r.is_error and error_of(r)["code"] == "server_unreachable"


# --- regressions from review (step 3, round 1) ---------------------------------------------------

def test_mcp_call_respects_wait_deadline(tts, tmp_path):
    t0 = time.monotonic()
    r = call("generate_speech", text="被卡住的工作。", output_path=str(tmp_path / "slow.wav"), wait_seconds=0.5)
    assert r.structured_content["status"] == "pending"
    assert time.monotonic() - t0 < 3


def test_error_after_submit_carries_job_id(tts, tmp_path, monkeypatch):
    async def down(*a, **k):
        raise client_module.ClientError("server_unreachable", "down")
    monkeypatch.setattr(tts, "job", down)
    out = tmp_path / "lost.wav"
    err = error_of(call("generate_speech", text="送出後斷線。", output_path=str(out)))
    assert err["code"] == "server_unreachable" and err["job_id"] and err["output_path"] == str(out)
    args = json.loads(err["recovery"][err["recovery"].index("{"):err["recovery"].rindex("}") + 1])
    assert args == {"job_id": err["job_id"], "output_path": str(out)}


def test_get_job_with_same_output_path_is_idempotent(tts, tmp_path):
    out = tmp_path / "same.wav"
    jid = call("generate_speech", text="同一路徑。", output_path=str(out), wait_seconds=0.2).structured_content["job_id"]
    GATE.set()
    first = call("get_speech_job", job_id=jid, output_path=str(out)).structured_content
    second = call("get_speech_job", job_id=jid, output_path=str(out)).structured_content
    assert first["status"] == "completed" and second == first


def test_concurrent_get_job_saves_once(tts, tmp_path):
    out = tmp_path / "concurrent.wav"
    jid = call("generate_speech", text="並行取回。", output_path=str(out), wait_seconds=0.2).structured_content["job_id"]

    async def go():
        async with mcp.Client(mcp_server.server) as c:
            calls = [c.call_tool("get_speech_job", {"job_id": jid, "wait_seconds": 5}) for _ in range(3)]
            asyncio.get_running_loop().call_later(0.3, GATE.set)
            return await asyncio.gather(*calls)
    results = asyncio.run(go())
    assert all(not r.is_error for r in results), [r.content for r in results]
    done = [r.structured_content for r in results if r.structured_content["status"] == "completed"]
    assert done and all(d == done[0] for d in done)


def test_recovery_after_restart_uses_job_format(tts, tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, "OUTPUT_DIR", tmp_path / "recovered")
    jid = call("generate_speech", text="重啟後取回。", output_path=str(tmp_path / "orig.flac"),
               wait_seconds=0.2).structured_content["job_id"]
    mcp_server.JOBS.clear()  # simulate an MCP server restart
    err = error_of(call("get_speech_job", job_id=jid, output_path=str(tmp_path / "wrong.wav")))
    assert err["code"] == "format_mismatch"
    GATE.set()
    done = call("get_speech_job", job_id=jid).structured_content
    assert done["status"] == "completed" and done["path"].endswith(".flac") and done["format"] == "flac"


def test_expected_failures_are_structured(tts, tmp_path, monkeypatch):
    GATE.set()
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        err = error_of(call("generate_speech", text="沒有權限。", output_path=str(locked / "sub" / "x.wav")))
        assert err["code"] == "io_error" and err["job_id"]
    finally:
        locked.chmod(0o700)
    assert error_of(call("generate_speech", text="x", output_path="/"))["code"] == "invalid_path"
    monkeypatch.setattr(client_module, "OUTPUT_DIR", client_module.Path("relative-dir"))
    assert error_of(call("generate_speech", text="x"))["code"] == "invalid_path"


def test_cli_non_utf8_file_and_quoted_fetch_command(tts, tmp_path, capsys):
    bad = tmp_path / "latin1.txt"
    bad.write_bytes("café".encode("latin-1"))
    assert cli.main(["say", "-f", str(bad), "-o", str(tmp_path / "x.wav")]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_input"
    out = tmp_path / "with space.wav"
    out.write_bytes(b"old")
    assert cli.main(["say", "稍後。", "-o", str(out), "--overwrite", "--no-wait", "--json"]) == 3
    fetch = json.loads(capsys.readouterr().out)["fetch_command"]
    assert f"'{out}'" in fetch and fetch.endswith("--overwrite")


# --- regressions from review (step 3, round 2) ---------------------------------------------------

def _slow_server(head: bytes, body_byte: bytes, before_head: bool):
    """Raw HTTP server sending one byte every 80 ms (headers and/or body). Returns (url, state)."""
    import socket
    import threading as th
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    state = {}

    def serve():
        conn, _ = srv.accept()
        conn.recv(4096)
        try:
            for b in (head if before_head else b""):
                conn.sendall(bytes([b]))
                time.sleep(0.08)
            if not before_head:
                conn.sendall(head)
            while True:
                conn.sendall(body_byte)
                time.sleep(0.08)
        except OSError:
            state["closed_at"] = time.monotonic()
        conn.close()
    state["thread"] = th.Thread(target=serve, daemon=True)
    state["thread"].start()
    return f"http://127.0.0.1:{srv.getsockname()[1]}", state


@pytest.mark.parametrize("phase", ["body", "headers", "gzip-body"])
def test_request_wall_clock_limit_in_every_phase(phase):
    """A response trickling in (body, headers, or gzip data that never decodes) is cancelled near the
    deadline, the connection is really closed (the server notices), and nothing keeps running."""
    import threading as th
    if phase == "headers":
        head = b"HTTP/1.1 200 OK\r\n" + b"X-Slow: " + b"a" * 400 + b"\r\n\r\n"
        url, state = _slow_server(head, b" ", before_head=True)
    elif phase == "gzip-body":
        head = b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 100000\r\n\r\n\x1f\x8b\x08\x00"
        url, state = _slow_server(head, b"\x00", before_head=False)
    else:
        head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n"
        url, state = _slow_server(head, b" ", before_head=False)
    threads_before = th.active_count()
    c = TTSClient(url)
    t0 = time.monotonic()
    with pytest.raises(ClientError) as e:
        asyncio.run(c.job("x", timeout=0.5))
    elapsed = time.monotonic() - t0
    assert e.value.code == "server_timeout" and elapsed < 0.8, elapsed
    state["thread"].join(timeout=2)  # the client must have closed the connection itself
    assert "closed_at" in state and state["closed_at"] - t0 < 1.5, "connection was not closed"
    assert th.active_count() <= threads_before, "a request is still running in the background"


def test_gzip_response_is_decoded_once():
    import gzip
    import socket
    import threading as th
    body = gzip.compress(json.dumps({"id": "x", "status": "running"}).encode())
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\n"
                     + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        conn.close()
    th.Thread(target=serve, daemon=True).start()
    c = TTSClient(f"http://127.0.0.1:{srv.getsockname()[1]}")
    assert asyncio.run(c.job("x")) == {"id": "x", "status": "running"}


def test_extensionless_path_repeat_is_idempotent(tts, tmp_path):
    base = tmp_path / "voice"
    jid = call("generate_speech", text="無副檔名。", output_path=str(base), format="flac",
               wait_seconds=0.2).structured_content["job_id"]
    GATE.set()
    first = call("get_speech_job", job_id=jid, output_path=str(base)).structured_content
    second = call("get_speech_job", job_id=jid, output_path=str(base)).structured_content
    assert first["path"] == str(base) + ".flac" and second == first


def test_concurrent_get_job_to_new_path_saves_once(tts, tmp_path):
    jid = call("generate_speech", text="換路徑並行。", output_path=str(tmp_path / "a.wav"),
               wait_seconds=0.2).structured_content["job_id"]
    other = tmp_path / "b.wav"

    async def go():
        async with mcp.Client(mcp_server.server) as c:
            calls = [c.call_tool("get_speech_job", {"job_id": jid, "output_path": str(other), "wait_seconds": 5})
                     for _ in range(3)]
            asyncio.get_running_loop().call_later(0.3, GATE.set)
            return await asyncio.gather(*calls)
    results = asyncio.run(go())
    assert all(not r.is_error for r in results), [r.content for r in results]
    done = [r.structured_content for r in results if r.structured_content["status"] == "completed"]
    assert done and all(d == done[0] for d in done) and done[0]["path"] == str(other)


def test_overwrite_retry_after_output_exists(tts, tmp_path):
    out = tmp_path / "race.wav"
    jid = call("generate_speech", text="寫入前被佔用。", output_path=str(out), wait_seconds=0.2).structured_content["job_id"]
    out.write_bytes(b"someone else")
    GATE.set()
    err = error_of(call("get_speech_job", job_id=jid))
    assert err["code"] == "output_exists" and err["job_id"] == jid
    done = call("get_speech_job", job_id=jid, overwrite=True).structured_content
    assert done["status"] == "completed" and out.read_bytes()[:4] == b"RIFF"


def test_recovery_failure_keeps_job_context(tts, monkeypatch):
    async def down(*a, **k):
        raise ClientError("server_unreachable", "down")
    monkeypatch.setattr(tts, "job", down)
    err = error_of(call("get_speech_job", job_id="abc123"))
    assert err["code"] == "server_unreachable" and err["job_id"] == "abc123" and "recovery" in err


def test_cli_write_failure_keeps_job_context(tts, tmp_path, capsys):
    GATE.set()
    locked = tmp_path / "ro"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert cli.main(["say", "寫不進去。", "-o", str(locked / "sub" / "x.wav"), "-q"]) == 1
    finally:
        locked.chmod(0o700)
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["code"] == "io_error" and err["job_id"] and "tts job" in err["fetch_command"]


def test_cli_non_utf8_stdin(tts, tmp_path, capsys, monkeypatch):
    import io

    class Stdin:
        buffer = io.BytesIO(b"\xff\xfe bad")

        def isatty(self):
            return False
    monkeypatch.setattr("sys.stdin", Stdin())
    assert cli.main(["say", "-o", str(tmp_path / "x.wav")]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_input"


def test_cancelled_save_then_retry_writes_once(tts, tmp_path, monkeypatch):
    """A call cancelled while downloading must leave nothing running; the retry saves exactly once."""
    out = tmp_path / "cancel.wav"
    jid = call("generate_speech", text="下載中被取消。", output_path=str(out), wait_seconds=0.2).structured_content["job_id"]
    GATE.set()
    original = tts._request
    hangs = {"left": 1}

    async def slow_audio(method, path, **kw):
        if path.endswith("/audio") and hangs["left"]:
            hangs["left"] -= 1
            await asyncio.sleep(5)  # cancelled below
        return await original(method, path, **kw)
    monkeypatch.setattr(tts, "_request", slow_audio)
    import threading as th
    writes, write_threads = [], []
    real_write = client_module.write_file

    def recording_write(*a):
        writes.append(a[0])
        write_threads.append(th.get_ident())
        return real_write(*a)
    monkeypatch.setattr(client_module, "write_file", recording_write)

    async def cancelled_then_retry():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(1.5):
                await mcp_server._get_job(jid, 30, None, False)
        assert not out.exists() and not mcp_server.JOBS[jid].lock.locked()
        return await mcp_server._get_job(jid, 30, None, False)
    done = asyncio.run(cancelled_then_retry())
    assert done["status"] == "completed" and writes == [out] and out.exists()
    # The write runs on the event-loop thread (not to_thread), so cancellation cannot orphan it.
    assert write_threads == [th.get_ident()]


def test_cli_job_save_failure_keeps_job_context(tts, tmp_path, capsys):
    GATE.set()
    jid = call("generate_speech", text="先完成。", output_path=str(tmp_path / "first.wav")).structured_content["job_id"]
    locked = tmp_path / "ro2"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert cli.main(["job", jid, "-o", str(locked / "sub" / "x.wav"), "-q"]) == 1
    finally:
        locked.chmod(0o700)
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["code"] == "io_error" and err["job_id"] == jid and "tts job" in err["fetch_command"]


# --- regressions from review (step 4, round 1) ---------------------------------------------------

def test_failed_job_is_terminal_not_resumable(tts, tmp_path, capsys):
    FAIL_GEN["on"] = True
    GATE.set()
    err = error_of(call("generate_speech", text="生成會失敗。", output_path=str(tmp_path / "f.wav")))
    assert err["job_status"] == "failed" and err["job_id"]  # the fake failure surfaces as internal_error
    assert "generate_speech again" in err["recovery"] and "get_speech_job" not in err["recovery"]
    assert cli.main(["say", "生成會失敗。", "-o", str(tmp_path / "g.wav"), "-q"]) == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["job_status"] == "failed" and err["job_id"] and "fetch_command" not in err


# --- regressions from review (step 4, round 2) ---------------------------------------------------

def test_pending_names_path_and_next_call(tts, tmp_path):
    out = tmp_path / "pend.wav"
    pend = call("generate_speech", text="等一下。", output_path=str(out), wait_seconds=0.2).structured_content
    assert pend["status"] == "pending" and pend["output_path"] == str(out)
    assert pend["next"] == {"tool": "get_speech_job", "arguments": {"job_id": pend["job_id"], "output_path": str(out)}}
    again = call("get_speech_job", job_id=pend["job_id"], wait_seconds=0.2).structured_content
    assert again["status"] == "pending" and again["output_path"] == str(out)


def test_unknown_job_says_resubmit(tts):
    err = error_of(call("get_speech_job", job_id="0123456789abcdef", wait_seconds=0.5))
    assert err["code"] == "unknown_job" and "generate_speech again" in err["recovery"]
    assert "get_speech_job" not in err["recovery"]


# --- regressions from review (step 4, round 3) ---------------------------------------------------

def test_following_next_finishes_a_regeneration(tts, tmp_path):
    out = tmp_path / "again.wav"
    out.write_bytes(b"old take")
    pend = call("generate_speech", text="重新生成。", output_path=str(out), overwrite=True,
                wait_seconds=0.2).structured_content
    assert pend["next"]["arguments"] == {"job_id": pend["job_id"], "output_path": str(out), "overwrite": True}
    GATE.set()
    # The same path without overwrite (what an agent may send) keeps generate_speech's permission ...
    done = call("get_speech_job", job_id=pend["job_id"], output_path=str(out)).structured_content
    assert done["status"] == "completed" and out.read_bytes() != b"old take"
    # ... and after an MCP restart, next.arguments alone are enough.
    mcp_server.JOBS.clear()
    out.write_bytes(b"old take")
    done = call("get_speech_job", **pend["next"]["arguments"]).structured_content
    assert done["status"] == "completed" and out.read_bytes() != b"old take"


def test_next_and_recovery_survive_quotes_in_paths(tts, tmp_path, monkeypatch):
    out = tmp_path / "actor's \"take\".wav"
    pend = call("generate_speech", text="引號。", output_path=str(out), wait_seconds=0.2).structured_content
    assert pend["next"]["arguments"]["output_path"] == str(out)

    async def down(*a, **k):
        raise client_module.ClientError("server_unreachable", "down")
    monkeypatch.setattr(tts, "job", down)
    err = error_of(call("get_speech_job", job_id=pend["job_id"], output_path=str(out), wait_seconds=0.5))
    args = json.loads(err["recovery"][err["recovery"].index("{"):err["recovery"].rindex("}") + 1])
    assert args["output_path"] == str(out)


def test_cli_job_unknown_job_has_no_fetch_command(tts, tmp_path, capsys, monkeypatch):
    GATE.set()
    jid = call("generate_speech", text="先完成。", output_path=str(tmp_path / "first.wav")).structured_content["job_id"]

    async def gone(*a, **k):
        raise client_module.ClientError("unknown_job", "Unknown job.")
    monkeypatch.setattr(tts, "_request", gone)  # the service restarted between status and download

    async def succeeded(self, job_id, **k):
        return {"id": job_id, "status": "succeeded", "format": "wav", "result": {"format": "wav"}}
    monkeypatch.setattr(tts, "job", succeeded.__get__(tts))
    assert cli.main(["job", jid, "-o", str(tmp_path / "x.wav"), "-q"]) == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["code"] == "unknown_job" and err["job_id"] == jid and "fetch_command" not in err


# --- final audit -------------------------------------------------------------------------------

def test_lost_submit_response_is_retried_without_a_second_job(tts, client, monkeypatch):
    original = tts._request
    calls = []

    async def lose_first_response(method, path, **kw):
        r = await original(method, path, **kw)
        calls.append(kw.get("headers", {}).get("Idempotency-Key"))
        if method == "POST" and len(calls) == 1:  # accepted by the server, response lost
            raise ClientError("server_unreachable", "connection reset")
        return r
    monkeypatch.setattr(tts, "_request", lose_first_response)
    job = asyncio.run(tts.submit("回應遺失。"))
    assert calls[0] and calls == [calls[0], calls[0]]
    assert list(client.app.state.scheduler.jobs) == [job["id"]]


def test_unknown_jobs_are_not_remembered(tts):
    for i in range(5):
        call("get_speech_job", job_id=f"{i:016x}", wait_seconds=0.2)
    assert mcp_server.JOBS == {}


def test_cli_job_wait_failure_keeps_job_context(tts, tmp_path, capsys, monkeypatch):
    async def down(*a, **k):
        raise ClientError("server_unreachable", "down")
    monkeypatch.setattr(tts, "job", down)
    out = tmp_path / "later.wav"
    assert cli.main(["job", "known123", "--wait", "-o", str(out), "-q"]) == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["job_id"] == "known123" and err["output_path"] == str(out) and "tts job known123" in err["fetch_command"]


def test_mcp_unknown_argument_is_an_explicit_error(tts, tmp_path):
    out = tmp_path / "fast.wav"
    r = call("generate_speech", text="快一點。", output_path=str(out), speed=1.2)
    err = error_of(r)
    assert r.is_error and err["code"] == "unsupported_parameter" and err["parameters"] == ["speed"]
    assert not out.exists() and not mcp_server.JOBS

    async def schemas():
        async with mcp.Client(mcp_server.server) as c:
            return [t.input_schema for t in (await c.list_tools()).tools]
    assert all(s.get("additionalProperties") is False for s in asyncio.run(schemas()))



def test_submit_retries_stay_within_the_deadline(monkeypatch):
    c = TTSClient("http://127.0.0.1:9")  # nothing listens: every attempt fails fast
    attempts = []
    original = c._request

    async def counted(*a, **k):
        attempts.append(1)
        return await original(*a, **k)
    c._request = counted
    with pytest.raises(ClientError):
        asyncio.run(c.submit("x", deadline=client_module.Deadline(0.3)))
    assert len(attempts) == 1  # less than 1 s left: no retry


def test_cli_prints_warnings(tts, tmp_path, capsys, monkeypatch):
    GATE.set()
    original = tts.save

    async def save_with_warning(job, path, overwrite):
        return await original(job, path, overwrite) | {"warnings": ["segment 0: short"]}
    monkeypatch.setattr(tts, "save", save_with_warning)
    assert cli.main(["say", "警告。", "-o", str(tmp_path / "w.wav")]) == 0
    assert "[tts] warning: segment 0: short" in capsys.readouterr().err


def test_cli_job_keeps_extensionless_path_until_format_known(tts, tmp_path, capsys, monkeypatch):
    async def down(*a, **k):
        raise ClientError("server_unreachable", "down")
    monkeypatch.setattr(tts, "job", down)
    out = tmp_path / "narration"
    assert cli.main(["job", "known123", "--wait", "-o", str(out), "-q"]) == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["output_path"] == str(out) and err["fetch_command"].endswith(f"-o {out}")


def test_cli_job_terminal_failure_names_path_without_fetch(tts, tmp_path, capsys):
    FAIL_GEN["on"] = True
    GATE.set()
    jid = asyncio.run(tts.submit("會失敗。"))["id"]
    out = tmp_path / "f.wav"
    assert cli.main(["job", jid, "--wait", "-o", str(out), "-q"]) == 1
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["job_status"] == "failed" and err["output_path"] == str(out) and "fetch_command" not in err


def test_generate_forgets_job_the_service_lost(tts, tmp_path, monkeypatch):
    async def gone(*a, **k):
        raise ClientError("unknown_job", "Unknown job.")
    monkeypatch.setattr(tts, "job", gone)
    err = error_of(call("generate_speech", text="服務重啟。", output_path=str(tmp_path / "x.wav"), wait_seconds=0.5))
    assert err["code"] == "unknown_job" and mcp_server.JOBS == {}


def test_stalled_submit_ends_within_the_request_floor():
    async def stall(request):
        await asyncio.sleep(30)
    c = TTSClient("http://testserver", transport=httpx.MockTransport(stall))
    t0 = time.monotonic()
    with pytest.raises(ClientError) as e:
        asyncio.run(c.submit("x", deadline=client_module.Deadline(0.05)))
    assert e.value.code == "server_timeout"
    assert time.monotonic() - t0 < 2 * client_module.MIN_REQUEST_SECONDS  # one attempt: no retry past the deadline


class SlowHTTP:
    """Wraps the client's httpx client: delays requests by path (the delay counts against timeouts)."""

    def __init__(self, inner, delays, seen):
        self.inner, self.delays, self.seen = inner, delays, seen

    async def request(self, method, path, **kw):
        self.seen.append(path)
        for prefix, seconds in self.delays.items():
            if path.startswith(prefix):
                await asyncio.sleep(seconds)
        return await self.inner.request(method, path, **kw)


def slow(tts, monkeypatch, delays):
    """Returns the list of request paths made (timing alone is flaky under load)."""
    original, seen = tts._client, []
    monkeypatch.setattr(tts, "_client", lambda: SlowHTTP(original(), delays, seen))
    return seen


def test_pending_after_deadline_skips_the_health_query(tts, tmp_path, monkeypatch):
    seen = slow(tts, monkeypatch, {"/v1/jobs": 0.1, "/health": 30})
    t0 = time.monotonic()
    r = call("generate_speech", text="期限已過。", output_path=str(tmp_path / "p.wav"), wait_seconds=0.05)
    assert r.structured_content["status"] == "pending"
    assert "/health" not in seen and time.monotonic() - t0 < 1.5


def test_requests_share_one_grace_period(tts, tmp_path, monkeypatch):
    seen = slow(tts, monkeypatch, {"/v1/jobs": 0.45})  # submission and every poll are slow
    r = call("generate_speech", text="都很慢。", output_path=str(tmp_path / "s.wav"), wait_seconds=0)
    assert r.structured_content["status"] == "pending"
    assert seen == ["/v1/jobs"]  # the submission used the grace; no poll or health query after it
    seen.clear()
    mcp_server.JOBS.clear()  # as after an MCP restart: the format is fetched, then reused
    r = call("get_speech_job", job_id=r.structured_content["job_id"], output_path=str(tmp_path / "s.wav"),
             wait_seconds=0)
    assert r.structured_content["status"] == "pending"
    assert len(seen) == 1 and seen[0].startswith("/v1/jobs/")


def test_known_final_snapshot_is_not_polled_again(tts):
    async def unreachable(*a, **k):
        raise ClientError("server_unreachable", "down")
    tts.job = unreachable
    for status in ("succeeded", "failed"):
        last = {"id": "j1", "status": status}
        assert asyncio.run(tts.wait("j1", client_module.Deadline(45), last=last)) is last


def test_cli_no_wait_with_timeout_skips_health_after_deadline(tts, tmp_path, capsys, monkeypatch):
    seen = slow(tts, monkeypatch, {"/v1/jobs": 0.45, "/health": 30})
    assert cli.main(["say", "不等。", "-o", str(tmp_path / "n.wav"), "--no-wait", "--timeout", "0", "--json"]) == 3
    assert seen == ["/v1/jobs"]
