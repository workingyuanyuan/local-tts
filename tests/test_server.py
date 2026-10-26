"""API + scheduler tests with a fake adapter (no GPU)."""
import gc
import time

from tests.fakes import FAIL_GEN, FAIL_LOAD, GATE, LOAD_HELD

def wait_status(c, job_id, statuses, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/v1/jobs/{job_id}").json()
        if j["status"] in statuses:
            return j
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {statuses}")


def test_queue_full_fifo_and_unfinished_audio(client):
    ids = [client.post("/v1/jobs", json={"input": f"第{i}個。"}).json()["id"] for i in range(3)]
    wait_status(client, ids[0], {"running"})
    r = client.post("/v1/jobs", json={"input": "太多了。"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "queue_full"
    assert r.headers["Retry-After"]
    j = client.get(f"/v1/jobs/{ids[2]}").json()
    assert j["status"] == "queued" and j["queue_position"] == 2
    r = client.get(f"/v1/jobs/{ids[1]}/audio")
    assert r.status_code == 409 and r.json()["error"] == {
        "code": "job_not_finished", "message": f"Job '{ids[1]}' is queued.", "job_status": "queued"}
    GATE.set()
    done = [wait_status(client, i, {"succeeded"}) for i in ids]
    assert [d["started"] for d in done] == sorted(d["started"] for d in done)  # FIFO
    assert all(d["finished"] and d["result"]["duration"] > 0 for d in done)
    assert client.get(f"/v1/jobs/{ids[0]}/audio").headers["content-type"] == "audio/wav"


def test_sync_timeout_returns_job_id(client):
    r = client.post("/v1/audio/speech", json={"input": "慢慢來。"})
    assert r.status_code == 504
    err = r.json()["error"]
    assert err["code"] == "sync_timeout"
    GATE.set()
    assert wait_status(client, err["job_id"], {"succeeded"})["status"] == "succeeded"


def test_idle_unload_then_reload(client):
    GATE.set()
    assert client.post("/v1/audio/speech", json={"input": "你好。"}).status_code == 200
    assert client.get("/health").json()["loaded_model"] == "moss-tts-local-v1.5"
    end = time.time() + 5
    while client.get("/health").json()["loaded_model"] and time.time() < end:
        time.sleep(0.1)
    assert client.get("/health").json()["loaded_model"] is None
    assert client.post("/v1/audio/speech", json={"input": "再一次。"}).status_code == 200


def test_load_failure_reported_and_releases_partial_load(client):
    GATE.set()
    FAIL_LOAD["on"] = True
    LOAD_HELD.clear()
    r = client.post("/v1/audio/speech", json={"input": "載入失敗。"})
    assert r.status_code == 500 and r.json()["error"]["code"] == "model_load_failed"
    gc.collect()
    assert LOAD_HELD and LOAD_HELD[0]() is None, "partially loaded object still referenced"
    FAIL_LOAD["on"] = False
    assert client.post("/v1/audio/speech", json={"input": "恢復了。"}).status_code == 200


def test_invalid_pause_rejected(client):
    for text in ["停[pause " + "9" * 400 + "s]頓。", "停[pause 61s]頓。", "停[pause 59s]" * 11 + "頓。"]:
        r = client.post("/v1/audio/speech", json={"input": text})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_pause"


def test_audio_of_job_failing_between_checks_keeps_structured_error(client):
    # Force the failure to land after the endpoint looked the job up: the response must still be
    # the job's structured error, never a 500 from reading empty metadata.
    GATE.set()
    FAIL_LOAD["on"] = True
    jid = client.post("/v1/jobs", json={"input": "會失敗。"}).json()["id"]
    wait_status(client, jid, {"failed"})
    r = client.get(f"/v1/jobs/{jid}/audio")
    assert r.status_code == 500 and r.json()["error"]["code"] == "model_load_failed"
    assert r.json()["error"]["job_id"] == jid


def test_audio_response_consistent_when_job_fails_during_read(client):
    # The job fails right after the endpoint takes its state snapshot: the response must reflect
    # that snapshot (409 running), not mix it with the later failure (formerly a 500 KeyError).
    sched = client.app.state.scheduler
    FAIL_GEN["on"] = True
    jid = client.post("/v1/jobs", json={"input": "讀取時失敗。"}).json()["id"]
    wait_status(client, jid, {"running"})
    original = type(sched).outcome

    def outcome_then_fail(self, job):
        snapshot = original(self, job)
        GATE.set()
        job.done.wait(5)
        assert job.status == "failed"
        return snapshot

    sched.outcome = outcome_then_fail.__get__(sched)
    r = client.get(f"/v1/jobs/{jid}/audio")
    assert r.status_code == 409 and r.json()["error"]["job_status"] == "running"
    del sched.outcome
    r = client.get(f"/v1/jobs/{jid}/audio")
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal_error"


def test_janitor_purges_expired_jobs_while_another_runs(client):
    sched = client.app.state.scheduler
    GATE.set()
    done_id = client.post("/v1/jobs", json={"input": "先完成。"}).json()["id"]
    wait_status(client, done_id, {"succeeded"})
    GATE.clear()
    running_id = client.post("/v1/jobs", json={"input": "長時間執行。"}).json()["id"]
    wait_status(client, running_id, {"running"})
    with sched.cond:
        sched.cfg.job_ttl_seconds = 0.1
        sched.janitor_interval = 0.05
        sched.cond.notify_all()
    end = time.time() + 3
    while time.time() < end and client.get(f"/v1/jobs/{done_id}").status_code != 404:
        time.sleep(0.05)
    assert client.get(f"/v1/jobs/{done_id}").json()["error"]["code"] == "unknown_job"
    assert client.get(f"/v1/jobs/{running_id}").json()["status"] == "running"
    GATE.set()


# --- final audit -------------------------------------------------------------------------------

def test_oversized_body_rejected_before_parsing(client):
    big = {"input": "短。", "padding": "x" * (300 * 1024)}
    r = client.post("/v1/jobs", json=big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "body_too_large"

    def chunks():  # no Content-Length: the limit applies while streaming
        for _ in range(40):
            yield b" " * 10000
    r = client.post("/v1/jobs", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "body_too_large"


def test_non_ascii_names_do_not_break_audio_headers(client):
    from urllib.parse import unquote
    GATE.set()
    jid = client.post("/v1/jobs", json={"input": "中文聲音。"}).json()["id"]
    wait_status(client, jid, {"succeeded"})
    client.app.state.scheduler.jobs[jid].meta["voice"] = "中文女聲"
    r = client.get(f"/v1/jobs/{jid}/audio")
    assert r.status_code == 200 and unquote(r.headers["X-TTS-Voice"]) == "中文女聲"


def test_crash_reports_segment(client):
    GATE.set()
    FAIL_GEN["on"] = True
    jid = client.post("/v1/jobs", json={"input": "第一句。"}).json()["id"]
    err = wait_status(client, jid, {"failed"})["error"]
    assert err["code"] == "internal_error" and err["segment"] == 0 and "segment 0" in err["message"]


def test_idempotency_key_returns_the_same_job(client):
    h = {"Idempotency-Key": "k-123"}
    first = client.post("/v1/jobs", json={"input": "只生成一次。"}, headers=h).json()
    again = client.post("/v1/jobs", json={"input": "只生成一次。"}, headers=h).json()
    assert again["id"] == first["id"] and len(client.app.state.scheduler.jobs) == 1
    r = client.post("/v1/jobs", json={"input": "壞鍵。"}, headers={"Idempotency-Key": "x" * 200})
    assert r.status_code == 400


def test_results_budget_refuses_new_jobs(client):
    GATE.set()
    jid = client.post("/v1/jobs", json={"input": "佔用記憶體。"}).json()["id"]
    wait_status(client, jid, {"succeeded"})
    client.app.state.scheduler.cfg.max_result_bytes = 1
    r = client.post("/v1/jobs", json={"input": "再一個。"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "results_full" and r.headers["Retry-After"]


def test_seed_range_validated(client):
    for seed in (-1, 2**64 - 1):
        r = client.post("/v1/jobs", json={"input": "種子。", "seed": seed})
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


def test_results_budget_also_applies_when_storing(client):
    sched = client.app.state.scheduler
    sched.cfg.max_result_bytes = 10  # one fake result (~4 KB of WAV) does not fit
    GATE.set()
    jid = client.post("/v1/jobs", json={"input": "放不下。"}).json()["id"]
    err = wait_status(client, jid, {"failed"})["error"]
    assert err["code"] == "results_full" and err["result_bytes"] > 10
    assert all(j.audio is None for j in sched.jobs.values())
