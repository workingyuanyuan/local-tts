"""Submit long-text jobs, poll until done, save audio and <out>/long_results.json.

Usage: step2_long.py <base_url> <out_dir> [case ...]   (default: all cases)
Env LONG_SUFFIX is appended to output names (e.g. "_nochain"). Exits non-zero if any job fails.
"""
import json
import os
import sys
import time

import httpx

BASE, OUT = sys.argv[1], sys.argv[2]
zh = open("tests/data/long_zh.txt").read()
en = open("tests/data/long_en.txt").read()
CASES = {
    "long_zh_novoice": {"input": zh, "language": "zh"},
    "long_zh_voice": {"input": zh, "language": "zh", "voice": "demo_zh_female"},
    "long_en_voice": {"input": en, "language": "en", "voice": "demo_en"},
    "long_en_novoice": {"input": en, "language": "en"},
}
selected = sys.argv[3:] or list(CASES)
SUFFIX = os.environ.get("LONG_SUFFIX", "")
jobs = {}
for name in selected:
    r = httpx.post(f"{BASE}/v1/jobs", json={**CASES[name], "seed": 7}, timeout=30)
    r.raise_for_status()
    jobs[name] = r.json()["id"]
pending, results = dict(jobs), {}
while pending:
    for name, jid in list(pending.items()):
        j = httpx.get(f"{BASE}/v1/jobs/{jid}").json()
        if j["status"] in ("succeeded", "failed"):
            del pending[name]
            results[name + SUFFIX] = j
            if j["status"] == "succeeded":
                open(f"{OUT}/{name}{SUFFIX}.wav", "wb").write(httpx.get(f"{BASE}/v1/jobs/{jid}/audio", timeout=60).content)
            print(name + SUFFIX, j["status"], json.dumps(j.get("result") or j.get("error"), ensure_ascii=False), flush=True)
    time.sleep(2)
json.dump(results, open(f"{OUT}/long_results{SUFFIX}.json", "w"), ensure_ascii=False, indent=2)
sys.exit(0 if all(j["status"] == "succeeded" for j in results.values()) else 1)
