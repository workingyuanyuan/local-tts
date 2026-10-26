"""Drive the real tts-mcp over stdio against the running service: short + long (pending) narration.
Usage: step3_mcp_stdio.py <out_dir>   Writes <out_dir>/mcp_stdio.json; exits non-zero on failure."""
import asyncio
import json
import os
import sys
import time

import mcp

OUT = os.path.abspath(sys.argv[1])
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = mcp.StdioServerParameters(command=os.path.join(ROOT, ".venv/bin/tts-mcp"))


async def main() -> int:
    log = []
    try:
        return await run(log)
    except BaseException as e:
        log.append({"failure": f"{type(e).__name__}: {e}"})
        raise
    finally:  # keep this run's record even when it fails
        json.dump(log, open(f"{OUT}/mcp_stdio.json", "w"), ensure_ascii=False, indent=2)


async def run(log: list) -> int:
    async with mcp.Client(SERVER) as c:
        tools = [t.name for t in (await c.list_tools()).tools]
        log.append({"tools": tools})
        r = await c.call_tool("generate_speech", {"text": "透過 MCP 生成的短旁白。", "language": "zh",
                                                  "output_path": f"{OUT}/mcp_short.wav"})
        log.append({"call": "generate_speech(short)", "is_error": r.is_error, "result": r.structured_content})
        assert not r.is_error and r.structured_content["status"] == "completed", r.content
        text = open("tests/data/long_en.txt").read()
        t0 = time.monotonic()
        r = await c.call_tool("generate_speech", {"text": text, "language": "en", "voice": "demo_en",
                                                  "output_path": f"{OUT}/mcp_long.mp3", "wait_seconds": 20})
        res = r.structured_content
        log.append({"call": "generate_speech(long, wait 20)", "elapsed": round(time.monotonic() - t0, 1), "result": res})
        assert res["status"] == "pending", res
        while res["status"] == "pending":
            t1 = time.monotonic()
            r = await c.call_tool("get_speech_job", {"job_id": res["job_id"], "wait_seconds": 45})
            assert not r.is_error, r.content
            res = r.structured_content
            log.append({"call": "get_speech_job", "elapsed": round(time.monotonic() - t1, 1), "result": res})
        assert res["status"] == "completed" and res["format"] == "mp3", res
    for e in log:
        print(json.dumps(e, ensure_ascii=False)[:300])
    return 0


sys.exit(asyncio.run(main()))
