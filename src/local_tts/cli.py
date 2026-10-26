"""tts: command-line client for tts-server.

  tts say "你好" -o /abs/path/hello.wav --language zh [--voice NAME] [--model ID] [--overwrite]
  tts say -f script.txt -o /abs/out.mp3        (-f - reads stdin)
  tts job <job_id> [--wait] [-o /abs/out.wav]  (status of / fetch a job, e.g. after --no-wait)
  tts models | tts voices [--model ID] | tts health

Exit codes: 0 done, 1 error (JSON on stderr), 2 usage error, 3 still pending (--no-wait / --timeout).
"""
import argparse
import asyncio
import json
import shlex
import sys

from local_tts.client import (DEFAULT_URL, ClientError, Deadline, TTSClient, as_client_error, check_target,
                              raise_job_error, resolve_output_path, resumable)

EXIT_ERROR, EXIT_PENDING = 1, 3


def _print(obj, as_json: bool, human: str) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2) if as_json else human)


def _progress(quiet: bool):
    last = [None]

    def show(job: dict) -> None:
        p = job.get("progress") or {}
        line = f"{job['status']}" + (f" (queue position {job['queue_position']})" if job.get("queue_position") else "")
        if p.get("segments_total"):
            line += f" {p['segments_done']}/{p['segments_total']} segments"
        if not quiet and line != last[0]:
            print(f"[tts] job {job['id']}: {line}", file=sys.stderr, flush=True)
            last[0] = line
    return show


def _human_result(r: dict) -> str:
    return (f"{r['path']}  ({r['duration_seconds']:.2f}s, {r['sample_rate']} Hz, {r['channels']} ch, "
            f"model={r['model']}, voice={r['voice'] or 'default'})")


def _show_result(r: dict, a) -> None:
    _print(r, a.json, _human_result(r))
    if not a.json:  # in JSON they are part of the result
        for w in r.get("warnings", []):
            print(f"[tts] warning: {w}", file=sys.stderr)


def _fetch_command(a, job_id: str, path) -> str:
    """A copy-pasteable command that resumes waiting for a job (keeps --url / --overwrite)."""
    cmd = ["tts", "job", job_id, "--wait", "-o", str(path)]
    if a.url != DEFAULT_URL:
        cmd += ["--url", a.url]
    if a.overwrite:
        cmd.append("--overwrite")
    return shlex.join(cmd)


def _read_text(a) -> "str | None":
    """Text from the argument, a file or stdin; always valid UTF-8 (else invalid_input)."""
    if a.file and a.file != "-":
        source, data = a.file, open(a.file, "rb").read()
    elif a.file == "-" or a.text == "-" or (a.text is None and not sys.stdin.isatty()):
        source, data = "stdin", sys.stdin.buffer.read()
    elif a.text is not None:
        source, data = "TEXT", a.text.encode("utf-8", "surrogateescape")  # argv undecodable bytes survive here
    else:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ClientError("invalid_input", f"{source} is not UTF-8 text ({e.reason}).") from None


async def cmd_say(c: TTSClient, a) -> int:
    text = _read_text(a)
    if text is None:
        print("tts say: give TEXT, -f FILE, or pipe text on stdin", file=sys.stderr)
        return 2
    path, fmt = resolve_output_path(a.output, a.format)
    check_target(path, a.overwrite)
    deadline = Deadline(a.timeout)  # shared by the submission and the wait
    job = await c.submit(text, model=a.model, voice=a.voice, language=a.language, fmt=fmt, seed=a.seed,
                         deadline=deadline)
    fetch = _fetch_command(a, job["id"], path)
    try:
        if not a.no_wait:
            job = await c.wait(job["id"], deadline, last=job, on_progress=_progress(a.quiet or a.json))
        if job["status"] in ("queued", "running"):
            pend = (await c.pending(job, text, deadline)) | {"output_path": str(path), "fetch_command": fetch}
            _print(pend, a.json, f"pending: job {job['id']} ({job['status']}); fetch later with:\n  {fetch}")
            return EXIT_PENDING
        r = await c.save(job, path, a.overwrite)
    except Exception as e:  # noqa: BLE001 - the job exists: say how to recover instead of resubmitting
        err = as_client_error(e)
        err.details.setdefault("job_id", job["id"])
        err.details["output_path"] = str(path)
        if resumable(err):  # a failed or forgotten job cannot be fetched, only resubmitted
            err.details["fetch_command"] = fetch
        raise err from None
    _show_result(r, a)
    return 0


async def cmd_job(c: TTSClient, a) -> int:
    # Until the job's format is known, errors name the path as given (an extensionless path gets
    # the job's extension later, so guessing .wav here could make the fetch command fail).
    target = a.output
    try:
        if a.output:  # validate the target before waiting
            resolve_output_path(a.output, None)
        if a.wait:
            job = await c.wait(a.job_id, Deadline(a.timeout), on_progress=_progress(a.quiet or a.json))
        else:
            job = await c.job(a.job_id)
        raise_job_error(job)
        if job["status"] == "succeeded" and a.output:
            path, _ = resolve_output_path(a.output, job["result"]["format"])
            target = str(path)
            r = await c.save(job, path, a.overwrite)
            _show_result(r, a)
            return 0
    except Exception as e:  # noqa: BLE001 - keep the job reference so the user can retry
        err = as_client_error(e)
        err.details.setdefault("job_id", a.job_id)
        if target:
            err.details["output_path"] = target
            if resumable(err):
                err.details["fetch_command"] = _fetch_command(a, a.job_id, target)
        raise err from None
    _print(job, True, "")
    return 0 if job["status"] == "succeeded" else EXIT_PENDING


async def cmd_models(c: TTSClient, a) -> int:
    ms = await c.models()
    _print(ms, a.json, "\n".join(
        f"{m['id']}{' (default)' if m['default'] else ''}{' [loaded]' if m['loaded'] else ''}: {m['description']}\n"
        f"  languages: {', '.join(m['languages'])}\n"
        f"  capabilities: {', '.join(k for k, v in m['capabilities'].items() if v)}" for m in ms))
    return 0


async def cmd_voices(c: TTSClient, a) -> int:
    vs = await c.voices(a.model)
    _print(vs, a.json, "\n".join(f"{v['name']} [{v['language']}] {v['description']} (models: {', '.join(v['models'])})"
                                 for v in vs) or "(no voices)")
    return 0


async def cmd_health(c: TTSClient, a) -> int:
    h = await c.health()
    _print(h, True, "")
    return 0


def main(argv=None) -> int:
    # Common options work before or after the subcommand (`tts --json health`, `tts say ... --json`):
    # the top-level parser holds the defaults; subcommands only override what is actually given.
    def add_common(p, defaults: bool) -> None:
        d = (lambda v: v) if defaults else (lambda v: argparse.SUPPRESS)
        p.add_argument("--url", default=d(DEFAULT_URL), help=f"server URL (default {DEFAULT_URL}; env LOCAL_TTS_URL)")
        p.add_argument("--json", action="store_true", default=d(False), help="machine-readable output")
        p.add_argument("-q", "--quiet", action="store_true", default=d(False), help="no progress on stderr")
    common = argparse.ArgumentParser(add_help=False)
    add_common(common, defaults=False)
    ap = argparse.ArgumentParser(prog="tts", description="Local TTS client (tts-server).")
    add_common(ap, defaults=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("say", parents=[common], help="synthesize text to an audio file")
    s.add_argument("text", nargs="?", help='text to speak ("-" = stdin)')
    s.add_argument("-f", "--file", help='read text from a file ("-" = stdin)')
    s.add_argument("-o", "--output", help="absolute output path (.wav/.flac/.mp3); default "
                   "~/.local/share/local-tts/output/<timestamp>.wav")
    s.add_argument("-l", "--language", help="language code or name, e.g. zh, en")
    s.add_argument("-v", "--voice", help="voice name to clone (see `tts voices`)")
    s.add_argument("-m", "--model", help="model id (see `tts models`)")
    s.add_argument("--format", choices=["wav", "flac", "mp3"], help="default: from extension, else wav")
    s.add_argument("--seed", type=int)
    s.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    s.add_argument("--no-wait", action="store_true", help="submit and print the job id (exit 3)")
    s.add_argument("--timeout", type=float, help="stop waiting after N seconds (exit 3, job keeps running)")
    s.set_defaults(func=cmd_say)

    j = sub.add_parser("job", parents=[common], help="show a job, or wait for it and save its audio")
    j.add_argument("job_id")
    j.add_argument("--wait", action="store_true")
    j.add_argument("--timeout", type=float)
    j.add_argument("-o", "--output", help="save the audio here when the job has succeeded")
    j.add_argument("--overwrite", action="store_true")
    j.set_defaults(func=cmd_job)

    sub.add_parser("models", parents=[common], help="list models").set_defaults(func=cmd_models)
    v = sub.add_parser("voices", parents=[common], help="list voices")
    v.add_argument("-m", "--model")
    v.set_defaults(func=cmd_voices)
    sub.add_parser("health", parents=[common], help="service status").set_defaults(func=cmd_health)

    a = ap.parse_args(argv)
    return asyncio.run(_run(a))


async def _run(a) -> int:
    c = TTSClient(a.url)
    try:
        return await a.func(c, a)
    except (ClientError, OSError, UnicodeError) as e:
        print(json.dumps({"error": as_client_error(e).to_dict()}, ensure_ascii=False), file=sys.stderr)
        return EXIT_ERROR
    finally:
        await c.aclose()


if __name__ == "__main__":
    sys.exit(main())
