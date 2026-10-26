# 第 1 步實測紀錄（2026-10-21）

## 固定版本

| 項目 | 值 |
|---|---|
| MOSS-TTS commit | `934d6826b084c46a0d033402174d5f8ac4ed2519`（2026-09-06），位於 `third_party/MOSS-TTS` |
| MOSS-TTS-Local-Transformer-v1.5 HF revision | `be7766a6735b98bd793f7c79fb720b4d0f5d13b8` |
| MOSS-Audio-Tokenizer-v2 HF revision | `f6e20e543b33d2c252a7ef71bdf8aa71e5ff9169` |
| Python | 3.12.15（uv 管理，`.venv/`） |
| torch / torchaudio | 2.9.1+cu128 |
| torchcodec | 0.8.1（**PyPI CPU 版**，非 cu128 版，見下） |
| transformers | 5.0.0 |
| ffmpeg | 8.0.1（apt） |
| uv | 0.13.4 |
| 驅動 | 616.92（CUDA 13.4），GPU sm_120 被 torch cu128 支援 |

完整套件清單：`docs/step1-requirements.lock.txt`（僅為環境快照，含絕對路徑 editable 安裝、未含來源與雜湊）。
`uv.lock` 列為第 2 步首項：torch/torchaudio 走 cu128 index、torchcodec 走 PyPI CPU 版，並驗證乾淨環境可重建。

systemd：`docs/step0-systemd.txt`（system 與 user manager 皆 `running`，無 failed unit）。

## 實測結果

腳本：`scripts/step1_smoke.py`（SDPA、模型 BF16、官方取樣參數、`max_new_tokens=4096`，未更動 `n_vq_for_inference`）；
執行：`scripts/step1_run.sh {bf16|fp32}`。原始日誌在 `outputs/step1/<dtype>/`（`smoke.log`、`nvidia-smi.csv`、`results.json`、`asr.jsonl`）。

**計時範圍**：processor（含克隆參考音檔編碼）＋ generate ＋ codec 解碼 ＋ CUDA 同步；不含模型載入與 WAV 寫檔。RTF = 耗時 / 實際音訊長度。

### Codec 精度（重要更正）

HF 快照的 remote processor（實際執行的程式碼）預設 `codec_weight_dtype="bf16"`，GitHub checkout 版預設 `"fp32"`。初版報告誤記為 fp32；現在腳本必須明確指定 `--codec-dtype`，並以 `get_codec_dtype_summary()` 記錄：

| codec_weight_dtype | encoder / decoder / quantizer | compute_dtype |
|---|---|---|
| bf16 | bf16 / bf16 / fp32 | bf16 |
| fp32 | fp32 / fp32 / fp32 | bf16 |

### 顯存（`torch.cuda` 數字含常駐權重；每個案例前 `reset_peak_memory_stats()`）

| | codec 載入後 | 模型載入後 | 生成峰值 allocated | 峰值 reserved | nvidia-smi 整卡取樣最大值（500 ms） |
|---|---:|---:|---:|---:|---:|
| **bf16** | 4.02 GiB | 11.77 GiB | 12.04 GiB | 12.28 GiB | 12908 MiB |
| **fp32** | 8.04 GiB | 15.79 GiB | 18.05 GiB | 18.71 GiB | 19490 MiB |

推論期間峰值比載入後多出：fp32 約 2.3 GiB、bf16 約 0.27 GiB（為整段推論峰值減載入後用量，未拆分 generate 與 codec 解碼；差異推測主要來自 fp32 codec，但未單獨量測）。僅 10–15 秒音訊；暫存可能隨音訊長度成長，第 2 步需實測長段的峰值並拆分各階段。

### RTF

| 案例 | bf16 長度 / RTF | fp32 長度 / RTF |
|---|---:|---:|
| zh（首次，含暖機） | 10.16 s / 2.18 | 10.32 s / 1.39 |
| en | 9.20 s / 0.68 | 8.24 s / 0.62 |
| clone_zh | 14.48 s / 0.65 | 14.96 s / 0.73 |
| zh（第二輪） | 11.92 s / 0.61 | 9.20 s / 0.64 |
| en（第二輪） | 9.76 s / 0.60 | 8.40 s / 0.59 |
| clone_zh（第二輪） | 14.48 s / 0.62 | 12.56 s / 0.56 |

- 本次暖機後兩者 RTF 接近（約 0.56–0.73），樣本數不足以判定差異；首次請求有明顯暖機成本（本次觀察 bf16 多約 15 s、fp32 多約 8 s）
- 樣本僅 6 段 8–15 秒短音訊，不足以推定長旁白耗時或吞吐，第 2 步需另測

### 輸出與內容驗證

- 全部為 WAV pcm_s16le、48 kHz、2 聲道，ffprobe 讀取與 ffmpeg 完整解碼正常
- ASR（`scripts/asr_check.py`，faster-whisper 1.2.1 large-v3-turbo，CPU int8；`step1_run.sh` 內含此步驟）：忽略標點／空白後 12 段中 11 段與原文完全一致；fp32/en 有一處 `this → the` 替換（無法僅憑轉錄判定來自 TTS 或 ASR）；逐段結果見各 `asr.jsonl`
- 克隆：參考音檔 `assets/ref/reference_zh.wav`（3.1 s，官方 demo，SHA256 與官方 repo `assets/audio/reference_zh.wav` 相同）。ASR 無法驗證音色相似度，需人耳確認
- 初版（未明確指定精度、實為 bf16）輸出保留在 `outputs/step1/_initial_run/`

### 建議

第 2 步預設 **codec bf16**：為 HF 官方 remote code 預設、顯存 12.3 GiB（fp32 需 18.7 GiB，長段解碼的餘裕明顯較小）、本次 RTF 接近、ASR 結果相當。adapter 中明確傳入 `codec_weight_dtype` 並可設定。

## 遇到的問題與解法

1. **sudo 需要密碼**：ffmpeg 由用戶手動 `apt-get install`
2. **pypi.nvidia.com 下載逾時**：設 `UV_HTTP_TIMEOUT=600 UV_HTTP_RETRIES=8` 後成功
3. **cu128 index 的 torchcodec（0.8.1+cu128）載入失敗**：缺 `libnppicc.so.12`（NVIDIA NPP，GPU 影像解碼用）。只需音訊 → 改用 PyPI 的 CPU 版 `torchcodec==0.8.1`，可搭配 FFmpeg 8
4. **remote processor 不接受 `revision`**：`from_pretrained` 會把 `revision` 轉傳給 codec 與 `ProcessorMixin.__init__`（報 `Unexpected keyword argument revision`）。解法：用 `snapshot_download(repo, revision=...)` 取得本機快照路徑，模型與 processor 都從路徑載入，codec 以 `codec_path=` 傳入
5. **codec 精度預設值不一致**（Codex 審查發現）：HF remote code 預設 bf16、GitHub 版預設 fp32 → 一律明確傳入 `codec_weight_dtype`
6. 警告（無害，暫不處理）：`torch_dtype` 已棄用（已改用 `dtype`）；tokenizer「incorrect regex pattern / fix_mistral_regex」警告（Qwen tokenizer 誤判，ASR 驗證輸出正確）；flash-attn 未安裝 → 回退 SDPA（符合預期）
7. faster-whisper 與新版 PyAV 不相容（`metadata_errors`）→ 驗證腳本改由 ffmpeg 解碼後傳 numpy
