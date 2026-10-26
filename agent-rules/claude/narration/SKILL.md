---
name: narration
description: Generate spoken audio with the local TTS service (MCP server local-tts). Use whenever a task's deliverable needs a voice — narration or voice-over for a video, slide deck, demo, tutorial or animation; reading teaching material, an article or a story aloud; podcast or audiobook-style audio; an audio preview of a script — including requests worded as 旁白、配音、朗讀、語音、唸出來、有聲、TTS、text to speech. Do not use it when the user only wants the script text, or asks for a different TTS provider.
---

# Narration with local TTS

A local GPU text-to-speech service (MOSS-TTS, 31 languages, 48 kHz stereo) is always available on this
machine. When a task needs spoken audio, generate it yourself: do not stop at writing a script and
asking the user to record or synthesize it elsewhere.

## Tools

MCP server `local-tts` (load them with ToolSearch if they are deferred):

- `mcp__local-tts__generate_speech` — text → audio file. Returns path, duration_seconds, sample_rate,
  channels, format, model, voice; or status `pending` with a job_id for long text.
- `mcp__local-tts__get_speech_job` — keep waiting for a pending job and write its file.
- `mcp__local-tts__list_voices`, `mcp__local-tts__list_models`, `mcp__local-tts__tts_status`.

If MCP is unavailable, use the CLI with a bounded wait:
`tts say -f script.txt -l zh -o /abs/path/narration.wav --json --timeout 45`.
Exit 0 = done, 1 = error (JSON on stderr), 3 = still running: the JSON has `job_id` and a
`fetch_command`; run that command with `--timeout 45` added, repeating while it exits 3. Never rerun
`tts say` for a job that exists.

## Workflow

1. **Write the script for the ear.** Short sentences; punctuation shapes the prosody. Write numbers,
   units, symbols and abbreviations the way they should be spoken when it is ambiguous ("2026年" is
   fine, "v1.5" → "一點五版" / "version one point five"). Use `[pause 1.0s]` for deliberate pauses and
   blank lines between paragraphs. For a mispronounced Chinese word, write it as tone-numbered pinyin
   separated by spaces, e.g. `您来自哪 zuo4 cheng2 shi4？`; prefer the common form of rare or variant
   characters (徑流, not 逕流). Save the script next to the audio (e.g.
   `narration.txt`) so it can be revised and regenerated.
2. **Choose model and voice.** Omit `model` (the default is the only one). Always pass `language`
   (`zh`, `en`, `ja`, …; the main language for mixed text).
   - Without `voice`, the model picks a voice per call. Within one call the service tries to keep it
     (later segments reuse the first ≥ 3 s of output as reference), but separate calls sound like
     different speakers. Whenever several clips or batches must sound like the same narrator, pass the
     same `voice` to every call: call `list_voices` and pick one matching the language.
   - The user's own reference recording must first be registered as a voice (file in
     `{{LOCAL_TTS_ROOT}}/assets/voices/`, entry in `{{LOCAL_TTS_ROOT}}/config/voices.yaml`, then
     `systemctl --user restart local-tts`). Ask before doing that.
3. **Write into the task's project directory** with an absolute `output_path`, following the project's
   existing layout (e.g. `assets/audio/`, else `audio/`). Use descriptive names (`scene01.wav`).
   Format: `.wav` for editing and video pipelines, `.mp3` for sharing, `.flac` for lossless and smaller.
   Never overwrite an existing file unless you are deliberately regenerating that file
   (`overwrite: true`); on `output_exists` choose another name or ask.
4. **Split by how the audio will be used.** Video or slides: one file per scene or slide, so each
   duration sets that scene's length. One continuous reading (article, story, podcast): one call with
   the full text; the service splits long text itself. Limits per call: 20,000 characters, each
   `[pause]` ≤ 60 s and all pauses ≤ 600 s in total (`input_too_long` / `invalid_pause` otherwise).
   Longer text: split at paragraph boundaries into batches with one fixed `voice`, generate them in
   order and join them (step 6). Calls run one at a time on one GPU, so make them sequentially.
5. **Long text and interrupted calls.** Status `pending` → call `get_speech_job` with the result's
   `next.arguments` (job_id, the original absolute `output_path`, and `overwrite` if you set it) until
   `completed`. Always pass the path: after an MCP restart the default location is outside the
   project. Never call `generate_speech` again for text that is still pending. When an error carries
   a `job_id`, follow its `recovery` field:
   - interrupted (connection, timeout, write error): `get_speech_job` with the arguments it gives;
   - `job_status: "failed"`, or `unknown_job`: the job is over; fix the cause if there is one and make
     a new `generate_speech` call.
   Synthesis runs at about 0.6× real time (a 5-minute narration takes about 3 minutes) plus up to
   ~20 s to load the model when idle.
6. **Hand the result to the next step.** Use the returned `duration_seconds` for timing, then verify
   the final product with `ffprobe`. Typical ffmpeg steps:
   - join clips: `ffmpeg -f concat -safe 0 -i list.txt -c copy narration.wav` (same format clips)
   - narration onto video: `ffmpeg -i video.mp4 -i narration.wav -map 0:v -map 1:a -c:v copy -c:a aac -b:a 192k out.mp4`
     (make the video at least as long as the narration; `-shortest` would cut speech off)
   - still image + narration: `ffmpeg -loop 1 -i slide.png -i slide.wav -c:v libx264 -tune stillimage -pix_fmt yuv420p -c:a aac -shortest slide.mp4`
7. **Report** the audio file paths, durations and the voice used.

## Errors

Errors are JSON `{"error": {"code", "message", ...}}`.
- `server_unreachable`: `systemctl --user start local-tts`, then `tts health`.
- A completed result with `warnings` (a segment stayed suspiciously short: a sentence may be missing): regenerate
  that file once (`overwrite: true`); if the warning persists, tell the user which file and passage.
- `queue_full`/`results_full`: wait a little and retry. `unknown_voice` / `unknown_model`: pick from `available`.
- `output_exists`: see step 3. `invalid_path`: the path must be absolute (Windows `C:\...` paths work).
- `segment_failed` / `segment_truncated` name the failing segment: rephrase or shorten that passage
  and submit again. `unknown_job` (the service restarted or the job expired): submit it again.
- `gpu_out_of_memory`: not enough GPU memory. Check `tts_status`, `nvidia-smi` and the logs, then tell
  the user what you found.
- Logs: `journalctl --user -u local-tts`.
