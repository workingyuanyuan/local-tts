"""Queueing test against a running server (expects queue_max=3).

Submits one job and waits until it runs, then fires 5 at once: exactly 3 must be accepted and 2
rejected with queue_full; all accepted jobs must succeed in submission (FIFO) order. Writes
<out>/concurrency.json and the audio files; exits non-zero on any failed assertion.
Usage: step2_concurrency.py <base_url> <out_dir>
"""
import concurrent.futures as cf
import json
import sys
import time

import httpx

BASE, OUT = sys.argv[1], sys.argv[2]
FORMATS = ["wav", "flac", "mp3"]


def body(i):
    b = {"input": f"這是第{i + 1}個同時送出的請求，用來測試佇列是否依序完成。", "language": "zh",
         "response_format": FORMATS[i % 3], "seed": i}
    if i % 2:
        b["voice"] = "demo_zh_female"
    return b


def submit(i):
    r = httpx.post(f"{BASE}/v1/jobs", json=body(i), timeout=30)
    return {"i": i, "http": r.status_code, "body": r.json()}


def wait(jid, statuses):
    while True:
        j = httpx.get(f"{BASE}/v1/jobs/{jid}").json()
        if j["status"] in statuses:
            return j
        time.sleep(0.2)


first = submit(0)
assert first["http"] == 202, first
wait(first["body"]["id"], {"running", "succeeded", "failed"})
with cf.ThreadPoolExecutor(5) as ex:
    burst = list(ex.map(submit, range(1, 6)))
subs = [first] + burst
accepted = [s for s in subs if s["http"] == 202]
rejected = [s for s in subs if s["http"] != 202]
results = {}
for s in accepted:
    j = wait(s["body"]["id"], {"succeeded", "failed"})
    results[s["i"]] = j
    if j["status"] == "succeeded":
        audio = httpx.get(f"{BASE}/v1/jobs/{j['id']}/audio", timeout=60).content
        open(f"{OUT}/concurrent_{s['i']}.{j['result']['format']}", "wb").write(audio)

by_created = [j["id"] for j in sorted(results.values(), key=lambda j: j["created"])]
by_started = [j["id"] for j in sorted(results.values(), key=lambda j: j["started"])]
report = {"submissions": subs, "jobs": results, "fifo": by_created == by_started}
json.dump(report, open(f"{OUT}/concurrency.json", "w"), ensure_ascii=False, indent=2)

assert len(accepted) == 4 and len(rejected) == 2, [(s["i"], s["http"]) for s in subs]
assert all(s["http"] == 503 and s["body"]["error"]["code"] == "queue_full" for s in rejected), rejected
assert all(j["status"] == "succeeded" for j in results.values()), results
assert report["fifo"], (by_created, by_started)
print(f"OK: 4 accepted, 2 queue_full, all succeeded in FIFO order; see {OUT}/concurrency.json")
