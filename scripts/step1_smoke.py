"""Step 1 smoke test: official MOSS-TTS-Local-Transformer-v1.5 example + VRAM / RTF measurement.

Usage: .venv/bin/python scripts/step1_smoke.py <out_dir> <ref_zh_wav> --codec-dtype {bf16,fp32} [--repeat]

RTF timing covers processor (incl. clone reference encoding) + generate + codec decode + CUDA sync;
it excludes model loading and WAV writing.
"""
import argparse
import json
import time
from pathlib import Path

import torch
import torchaudio
from huggingface_hub import snapshot_download
from transformers import AutoModel, AutoProcessor

torch.backends.cuda.enable_cudnn_sdp(False)
torch.backends.cuda.enable_flash_sdp(True)
torch.backends.cuda.enable_mem_efficient_sdp(True)
torch.backends.cuda.enable_math_sdp(True)

MODEL_ID = "OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5"
MODEL_REV = "be7766a6735b98bd793f7c79fb720b4d0f5d13b8"
CODEC_REV = "f6e20e543b33d2c252a7ef71bdf8aa71e5ff9169"
MAX_NEW_TOKENS = 4096

ap = argparse.ArgumentParser()
ap.add_argument("out_dir", type=Path)
ap.add_argument("ref_zh")
# The HF remote processor defaults codec weights to bf16 while the GitHub copy defaults to fp32,
# so always pass it explicitly.
ap.add_argument("--codec-dtype", required=True, choices=["bf16", "fp32"])
ap.add_argument("--repeat", action="store_true")
args = ap.parse_args()
out_dir = args.out_dir
ref_zh = args.ref_zh
out_dir.mkdir(parents=True, exist_ok=True)
device = "cuda"
GiB = 1024**3


def mem(tag):
    torch.cuda.synchronize()
    print(f"[MEM] {tag}: allocated={torch.cuda.memory_allocated() / GiB:.2f} GiB "
          f"reserved={torch.cuda.memory_reserved() / GiB:.2f} GiB "
          f"peak={torch.cuda.max_memory_allocated() / GiB:.2f} GiB", flush=True)


t0 = time.perf_counter()
# The remote processor rejects `revision` (and would forward it to the codec), so pin both
# repos by resolving their snapshot paths and loading from those.
model_path = snapshot_download(MODEL_ID, revision=MODEL_REV)
codec_path = snapshot_download("OpenMOSS-Team/MOSS-Audio-Tokenizer-v2", revision=CODEC_REV)
processor = AutoProcessor.from_pretrained(model_path, trust_remote_code=True, codec_path=codec_path,
                                          codec_weight_dtype=args.codec_dtype)
processor.audio_tokenizer = processor.audio_tokenizer.to(device)
codec_dtypes = processor.audio_tokenizer.get_codec_dtype_summary()
print("[CODEC]", json.dumps(codec_dtypes), flush=True)
mem("after codec load")
model = AutoModel.from_pretrained(
    model_path, trust_remote_code=True,
    attn_implementation="sdpa", dtype=torch.bfloat16,
).to(device)
model.eval()
print(f"[LOAD] {time.perf_counter() - t0:.1f}s", flush=True)
mem("after model load")

text_zh = ("大家好，欢迎收看本期节目。今天我们要聊一聊本地语音合成服务，"
           "它可以让智能助手根据任务需要，主动为视频和演示文稿生成旁白。")
text_en = ("Welcome to this short demo. Today we will build a local text to speech service, "
           "so that coding agents can generate narration for videos and slides on demand.")

cases = [
    ("zh", [processor.build_user_message(text=text_zh, language="Chinese")]),
    ("en", [processor.build_user_message(text=text_en, language="English")]),
    ("clone_zh", [processor.build_user_message(text=text_zh, reference=[ref_zh], language="Chinese")]),
]
if args.repeat:
    cases += [(n + "_run2", c) for n, c in cases]

results = []
with torch.no_grad():
    for name, conv in cases:
        torch.cuda.reset_peak_memory_stats()
        t = time.perf_counter()
        batch = processor([conv], mode="generation")
        outputs = model.generate(
            input_ids=batch["input_ids"].to(device),
            attention_mask=batch["attention_mask"].to(device),
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=True,
            audio_temperature=1.7,
            audio_top_p=0.8,
            audio_top_k=25,
            audio_repetition_penalty=1.0,
        )
        t_gen = time.perf_counter() - t
        messages = processor.decode(outputs)
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - t
        audio = messages[0].audio_codes_list[0]
        sr = processor.model_config.sampling_rate
        path = out_dir / f"{name}.wav"
        torchaudio.save(str(path), audio, sr)
        dur = audio.shape[-1] / sr
        r = dict(name=name, seconds_audio=round(dur, 2), gen_s=round(t_gen, 2), total_s=round(elapsed, 2),
                 rtf=round(elapsed / dur, 3), frames_est=round(dur * 12.5),
                 channels=audio.shape[0], sr=sr,
                 peak_alloc_gib=round(torch.cuda.max_memory_allocated() / GiB, 2),
                 peak_reserved_gib=round(torch.cuda.max_memory_reserved() / GiB, 2))
        print("[RESULT]", json.dumps(r), flush=True)
        results.append(r)

(out_dir / "results.json").write_text(json.dumps(
    dict(codec_weight_dtype=args.codec_dtype, codec_dtypes=codec_dtypes, results=results), indent=2))
