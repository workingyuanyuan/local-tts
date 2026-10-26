"""Summarize the local-tts usage in an agent transcript.

Usage: step4_evidence.py claude <stream.jsonl> | codex <events.jsonl>
       step4_evidence.py pairs <evidence.json> <dir>   write each script to <dir>/<audio name>.txt and
                                                       print "<script>\\t<audio>" lines
Prints JSON:
  calls             local-tts tool calls per tool, counting every attempt (also failed or unfinished)
  speech_completed  generate_speech/get_speech_job results with status "completed"
  files             unique paths of those results, in completion order
  scripts           {path: text} the final version of each file was generated from (a job is followed
                    through pending results and errors that carry its job_id)
  tool_errors       local-tts tool calls that ended in an error
  shell_tts         shell commands that reach the service another way (tts CLI, HTTP API)
  skills            Claude Skill invocations
  unparsed_results  successful speech results without a readable status (the checker fails on them)
Exits non-zero when the transcript cannot be parsed or is incomplete.
"""
import json
import os
import re
import sys
from collections import Counter

SPEECH = {"generate_speech", "get_speech_job"}
# `tts [common options] <subcommand>`, the HTTP API, or the Python modules
SHELL_TTS = re.compile(r"(^|[\s;&|(`/'\"])tts(\s+-{1,2}[\w-]+(=\S+|\s+(?!(say|job|health|models|voices)\b)"
                       r"[^\s-]\S*)?)*\s+(say|job|health|models|voices)\b"
                       r"|:8765|/v1/(audio|jobs)|local_tts|tts-mcp")


def _json_in(text: str) -> dict:
    """The JSON object in a tool result or error text (MCP errors carry a prefix before it)."""
    try:
        r = json.loads(text[text.index("{"):]) if "{" in text else {}
    except ValueError:
        return {}
    return r if isinstance(r, dict) else {}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # [{"type": "text", "text": ...}, ...]
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return json.dumps(content) if content is not None else ""


class Tracker:
    def __init__(self):
        self.calls, self.errors, self.files, self.scripts, self.jobs = Counter(), 0, [], {}, {}
        self.shell, self.skills, self.completed, self.unparsed = [], [], 0, 0

    def result(self, tool: str, text, r: dict, is_error: bool) -> None:
        """Record one finished local-tts call; text is the generate_speech input (None otherwise)."""
        if is_error:
            self.errors += 1
            r = r.get("error", r)
        if tool not in SPEECH:
            return
        if not is_error and "status" not in r:  # a speech result we could not read
            self.unparsed += 1
        if r.get("job_id") and text:  # pending, completed or an error after submit
            self.jobs[r["job_id"]] = text
        if not is_error and r.get("status") == "completed":
            self.completed += 1
            path = r.get("path")
            if path in self.files:
                self.files.remove(path)
            self.files.append(path)
            self.scripts[path] = self.jobs.get(r.get("job_id")) or text

    def command(self, cmd: str) -> None:
        if SHELL_TTS.search(cmd or ""):
            self.shell.append(cmd)

    def summary(self) -> dict:
        return dict(calls=dict(self.calls), speech_completed=self.completed, files=self.files,
                    scripts={k: v for k, v in self.scripts.items() if v}, tool_errors=self.errors,
                    shell_tts=self.shell, skills=self.skills, unparsed_results=self.unparsed)


def claude(path: str) -> dict:
    t, pending, texts, finished = Tracker(), {}, {}, False
    for line in open(path, encoding="utf-8"):
        ev = json.loads(line)  # every line of stream-json is an event; anything else is a parse failure
        finished = finished or ev.get("type") == "result"
        msg = ev.get("message")
        for block in (msg.get("content") or [] if isinstance(msg, dict) else []):  # some events carry a str
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name, args = block.get("name", ""), block.get("input") or {}
                if name == "Skill":
                    t.skills.append(args.get("skill"))
                elif name == "Bash":
                    t.command(args.get("command"))
                elif name.startswith("mcp__local-tts__"):
                    tool = name.removeprefix("mcp__local-tts__")
                    t.calls[tool] += 1
                    pending[block["id"]] = tool
                    texts[block["id"]] = args.get("text") if tool == "generate_speech" else None
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in pending:
                tool = pending.pop(block["tool_use_id"])
                r = _json_in(_text(block.get("content")))
                t.result(tool, texts.pop(block["tool_use_id"]), r, bool(block.get("is_error")))
    if not finished:
        sys.exit(f"{path}: no final 'result' event (truncated or not a Claude stream-json transcript)")
    t.errors += len(pending)  # calls that never returned
    return t.summary()


def codex(path: str) -> dict:
    t, started, done, kinds = Tracker(), {}, set(), set()
    for line in open(path, encoding="utf-8"):
        if not line.strip():
            continue
        ev = json.loads(line)  # `codex exec --json` writes only events to stdout
        kinds.add(ev.get("type"))
        item = ev.get("item") or {}
        kind = ev.get("type")
        if item.get("type") == "command_execution" and kind == "item.started":
            t.command(item.get("command"))
        if item.get("type") != "mcp_tool_call" or item.get("server") != "local-tts":
            continue
        tool, iid = item.get("tool", ""), item.get("id")
        if iid not in started:  # count each attempt once, whether it ever completes or not
            started[iid] = tool
            t.calls[tool] += 1
        if kind != "item.completed" or iid in done:
            continue
        done.add(iid)
        text = (item.get("arguments") or {}).get("text") if tool == "generate_speech" else None
        if item.get("error") or item.get("status") != "completed":
            err = item.get("error")
            t.result(tool, text, _json_in(err.get("message", "") if isinstance(err, dict) else _text(err)), True)
            continue
        result = item.get("result") or {}
        r = result.get("structured_content") or _json_in(_text(result.get("content")))
        if isinstance(r, dict) and isinstance(r.get("result"), dict):
            r = r["result"]
        t.result(tool, text, r, bool(result.get("is_error")))
    if not {"thread.started", "turn.completed"} <= kinds:
        sys.exit(f"{path}: missing thread.started/turn.completed (truncated or not a Codex --json transcript)")
    t.errors += len(set(started) - done)  # calls that never finished
    return t.summary()


def pairs(evidence: str, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    for audio, text in json.load(open(evidence, encoding="utf-8")).get("scripts", {}).items():
        src = os.path.join(out_dir, os.path.basename(audio) + ".txt")
        open(src, "w", encoding="utf-8").write(text)
        print(f"{src}\t{audio}")


if __name__ == "__main__":
    kind = sys.argv[1]
    if kind == "pairs":
        pairs(sys.argv[2], sys.argv[3])
    else:
        print(json.dumps({"claude": claude, "codex": codex}[kind](sys.argv[2]), ensure_ascii=False))
