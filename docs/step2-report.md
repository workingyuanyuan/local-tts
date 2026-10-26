# 第 2 步實測紀錄：tts-server（2026-10-21）

## 實作內容

| 模組 | 說明 |
|---|---|
| `pyproject.toml` / `uv.lock` | 套件鎖定：torch/torchaudio 走 cu128 index（`explicit`），torchcodec 走 PyPI CPU 版；`moss-tts` 以 editable path 依賴 `third_party/MOSS-TTS` |
| `config/{server,models,voices}.yaml` | 服務設定、模型註冊表（固定 revision、codec bf16、官方取樣參數）、聲音註冊表 |
| `src/local_tts/adapters/` | adapter 介面與 `moss_local`（依官方範例；snapshot 路徑固定 revision；參考音檔編碼快取；部分載入可被 `unload()` 清除） |
| `src/local_tts/text.py` | 句界切段、`[...]` 標記不拆、段首尾 `[pause]` 轉靜音；截斷重切的子段走同樣處理 |
| `src/local_tts/engine.py` | ModelManager（互斥鎖、單一模型、切換先卸載、載入失敗不殘留 traceback）與長文管線 |
| `src/local_tts/server/scheduler.py` | 單一 GPU worker；一把 condition lock 保護佇列與所有工作狀態；閒置卸載與 submit 互斥；週期清理過期工作 |
| `src/local_tts/server/app.py` | FastAPI：`/health`、`/v1/models`、`/v1/voices`、`/v1/audio/speech`、`/v1/jobs*` |
| `deploy/local-tts.service` | systemd user service（`HF_HUB_OFFLINE=1`），已安裝並 enable |
| `tests/` | 22 個測試（假 adapter，不需 GPU）：切段、截斷／重試、Tensor 參考、停頓保留、排程、API 錯誤、閒置卸載、載入失敗釋放 |
| `scripts/step2_run.sh` / `step2_eval.sh` | 完整實測與評估；所有原始紀錄存在輸出目錄 |

## 可重現性

- `uv.lock` 由 `pyproject.toml` 產生：torch/torchaudio `2.9.1+cu128` 來源為 `download.pytorch.org/whl/cu128`，torchcodec `0.8.1` 與其餘套件來源為 PyPI
- 乾淨環境重建：`UV_PROJECT_ENVIRONMENT=<新目錄> uv sync --frozen` 成功；torch CUDA 可用、torchcodec 可載入音訊、transformers 5.0.0。套件清單與 `.venv` 相同，只差重建開始後才加入鎖檔的 dev 依賴（pytest、pluggy、iniconfig）
- 模型與 codec 的 HF revision 固定在 `config/models.yaml`；服務以 `HF_HUB_OFFLINE=1` 執行
- 評估工具也固定版本：faster-whisper 1.2.1、zhconv 1.4.3、speechbrain 1.1.2，評估模型的 HF revision 寫在腳本中

## 實測結果（`scripts/step2_run.sh outputs/step2/final`，第 2 輪修正後重跑）

原始紀錄全部在 `outputs/step2/final/`（上一輪的完整紀錄保留在 `outputs/step2/round2/`；`outputs/step2/` 頂層為最早一輪的產物）：`server*.log`（服務日誌）、`nvidia-smi.csv`（500 ms 取樣）、`concurrency.json`、`long_results*.json`（每個工作的完整狀態與結果）、`truncation.json`、`health_after_*.json`、`asr_*.jsonl`、`speaker_consistency.jsonl`、`ffprobe.txt`。測試期間暫停 systemd 服務，用 `queue_max: 3`、`idle_unload_seconds: 60` 的測試設定（`outputs/step2/final/config*/`）。

### 並行與佇列（`concurrency.json`，腳本含斷言）

先送 1 個工作並等它開始執行，再同時送 5 個：3 個進入佇列、2 個立即回 `503 queue_full`。接受的 4 個全部成功，伺服器端 `created` 與 `started` 順序一致（FIFO）。wav／flac／mp3 各至少一個，皆可被 ffmpeg 完整解碼（`ffprobe.txt`）。

### 長文（`long_results*.json`）

| 案例 | 段數 | 音訊長度 | 生成耗時 | RTF | 重試／切分 | ASR 錯誤率 | 漏句（逐句檢查） |
|---|---:|---:|---:|---:|---|---:|---|
| 中文（繁體原文，25 句）無 voice | 8 | 133.4 s | 80.1 s | 0.60 | 0／0 | CER 0.6% | 無 |
| 中文 demo_zh_female | 8 | 169.0 s | 103.7 s | 0.61 | 0／0 | CER 1.0% | 無 |
| 英文（23 句）demo_en | 7 | 111.4 s | 67.8 s | 0.61 | 0／0 | WER 2.1% | 無 |
| 英文 無 voice | 7 | 108.5 s | 67.2 s | 0.62 | 0／0 | WER 2.1% | 無 |

（固定 seed，音訊長度與上一輪完全相同；耗時略有差異。）

- 顯存：`nvidia-smi.csv` 為整卡用量（含其他程序；測試開始前基線 779 MiB），500 ms 取樣、無取樣空洞。取樣觀測最大值 14,388 MiB，出現在第 4 個長文完成約 28 秒後、仍在閒置期間，卸載後回到 1,735 MiB。推估是 PyTorch caching allocator 保留已配置的記憶體（沒有程序層級的時序紀錄，無法確證）；若其他程序用量維持在基線，服務約佔 13.3 GiB（推估）。上一輪（`round2/`）取樣最大值為 13,038 MiB
- ASR：`scripts/asr_compare.py`（faster-whisper 1.2.1、`mobiuslabsgmbh/faster-whisper-large-v3-turbo@0a363e9`、VAD、繁→簡字元轉換、阿拉伯數字→中文數字）。`missing_sentences` 逐句檢查：任何長度的句子，對齊後匹配不到 60% 即列出（可抓到單字句如 "Stop."）。剩餘差異為零星的字詞替換（`assistants→assistance` 這類同音字，以及 `過去→或許` 等）與數字寫法；只看轉錄無法單獨歸因於 TTS 或 ASR（最終稽核時的人耳聆聽認為 `過去→或許`、`請求`、`閒置` 等是 TTS 錯讀，見 README「已知限制」）
- 單一 `/v1/audio/speech` 同步請求也能完成 149 s 長文（第一輪測試）

### 強制截斷（`truncation.json`、`server_trunc.log`）

將 `max_new_tokens` 降為 150 frames（約 12 s），送出約 27 秒、含 `[pause 2s]` 的中文段落：回應標頭 `X-TTS-Splits: 3`（腳本斷言 ≥ 1），日誌記錄 3 次「truncated at 150 frames; splitting」（含第二層切分），最終 200 成功，27.1 s 音訊，ASR CER 0%、無漏句。此路徑使用真實模型的 Tensor 參考，驗證審查發現的 Tensor 布林判斷錯誤已修正。

### 跨段一致性（未指定 voice）：維持「第一段作為後續段參考」

`scripts/speaker_consistency.py`（speechbrain 1.1.2、`speechbrain/spkrec-ecapa-voxceleb@0f99f2d`）：每個完整 6 秒視窗與開頭視窗的餘弦相似度，衡量「相對開頭音色的偏移」，不是每對段落的比較。

| 案例 | 平均 | 最低 |
|---|---:|---:|
| 中文 無 voice，`first_segment`（預設） | 0.82 | 0.79 |
| 中文 無 voice，`none` | 0.35 | 0.20 |
| 中文 demo_zh_female | 0.82 | 0.73 |
| 英文 無 voice，`first_segment`（預設） | 0.81 | 0.77 |
| 英文 無 voice，`none` | 0.33 | 0.12 |
| 英文 demo_en | 0.62 | 0.53 |

不串接時，第一段之後的音色明顯偏離開頭；串接後的一致性與指定 voice 相當或更高。兩輪實測結論相同。

### 閒置卸載（`server.log`、`health_after_*.json`）

閒置 60 s → `idle for 60s; unloading`；卸載後 PyTorch allocated 0.01 GiB、整卡 1,735 MiB。卸載完成約 1 秒後再次請求（腳本斷言 HTTP 200），自動重新載入（5.6 s，權重已在 page cache）並成功生成 5.2 s 音訊。

### 錯誤回報

全部為 `{"error": {"code", "message", ...}}`，並附可用清單或工作 ID。HTTP 層級的 `unknown_model`、`unknown_voice`、`unsupported_parameter`、`unsupported_format`、`unsupported_language`、`empty_input`、`invalid_request`、`unknown_job` 已實際驗證；`queue_full`（503 + Retry-After）、`job_not_finished`（409 + `job_status`）、`sync_timeout`（504 + `job_id`）、`invalid_pause`（單一停頓 > 60 s、總和 > 600 s 或非有限值）、`model_load_failed`、`segment_truncated`／`segment_failed`（含段落索引）由測試涵蓋。

### systemd

`~/.config/systemd/user/local-tts.service` 已安裝並 `enable --now`；經服務生成 mp3 成功，`journalctl --user -u local-tts` 有完整日誌，`restart` 正常；實測腳本結束後自動恢復服務。

## Codex 審查與修正（第 1 輪：`docs/step2-codex-review-1.md`）

| # | 嚴重度 | 問題 | 修正 |
|---|---|---|---|
| 1 | 阻擋 | 截斷重切時 `ref_out or ref` 對多元素 Tensor 做布林判斷而例外 | 重構為 `_sequence`／`_render` 共用路徑，參考以 `is None` 判斷；測試改用 Tensor 參考；以真實模型強制截斷驗證 |
| 2 | 阻擋 | 載入失敗時，例外 traceback 經工作紀錄持有部分載入的 GPU 物件 | adapter 立即把物件存到 `self`；錯誤在 except 外重新建立後拋出（無 `__context__`）；工作紀錄只存序列化錯誤；測試以 weakref 驗證釋放 |
| 3 | 阻擋 | `\S+` 讓「「」吞掉後面整串中文，長引文無法切細 | 硬切改為「單一 CJK 字元 或 非 CJK 非空白字串」；測試涵蓋 2,000 字引文 |
| 4 | 阻擋 | 截斷重切遺失邊界 `[pause]`，且子段不參與第一段參考 | `split_finer` 回傳 Segment（邊界停頓轉靜音）；第一個合格子段立即成為後續參考；測試涵蓋 |
| 5 | 阻擋 | `job_not_finished` 的 `status=` 與建構子參數衝突 → 500 | 建構子參數改名 `http_status`，明細改為 `job_status`；測試涵蓋 |
| 6 | 建議 | 閒置卸載與 submit 競態；狀態快照不一致 | 自建 deque＋condition lock：出列與標記 running 原子化；卸載決策與卸載期間持鎖；`snapshot()` 在鎖內產生 |
| 7 | 建議 | TTL 只在請求時清理；同步等待無上限 | 清理執行緒（見第 2 輪 #3）；`sync_timeout_seconds`（預設 1800）逾時回 504 + `job_id`（工作繼續）。執行中的工作無法安全中斷，但每段生成有 frame 上限，不會無限執行 |
| 8 | 建議 | 實測缺原始紀錄、腳本無斷言 | `step2_run.sh` 保存全部原始紀錄；並行腳本改為決定性流程並斷言數量、錯誤碼、FIFO |
| 9 | 建議 | ASR 漏判短句；說話人評估結論過度、版本未固定 | 新增逐句 `missing_sentences`；說話人評估改述為「相對開頭的偏移」；固定 faster-whisper／speechbrain 版本與兩個評估模型的 HF revision |

## Codex 審查與修正（第 2 輪：`docs/step2-codex-review-2.md`）

第 1 輪的 #1–#6、#8 經核實已修正；以下為第 2 輪新發現：

| # | 嚴重度 | 問題 | 修正 |
|---|---|---|---|
| 1 | 阻擋 | `/v1/jobs/{id}/audio` 先查錯誤、再查完成，工作在兩者之間失敗 → 讀空 meta 而 500 | `Scheduler.outcome()` 在鎖內一次取得狀態／錯誤／音訊／meta；端點只依這份快照回應 |
| 2 | 阻擋 | 無空白的長串（泰文）被當成單一單位，無法切細 | 硬切時若單一單位仍超過上限，依字元切塊（留 5% 餘裕）；測試涵蓋 2,400 字泰文 |
| 3 | 建議 | worker 區域變數持有最後一個工作，TTL 無法釋放音訊；長工作期間不清理 | 執行後清掉引用；新增每 30 秒清理一次的 janitor 執行緒 |
| 4 | 建議 | `max(125, min(cap, frames))` 可超過設定上限 | 改為 `min(cap, max(125, frames))`；測試涵蓋 |
| 5 | 建議 | `[pause]` 秒數未驗證（超長數字 → inf、極大值配置大量記憶體） | 單一停頓 ≤ 60 s、總和 ≤ 600 s、必須為有限值，否則 `invalid_pause`（400）；測試涵蓋 |
| 6 | 建議 | 驗收腳本可能漏報失敗 | 解碼／ffprobe 失敗即讓評估失敗；卸載後請求斷言 200；強制截斷斷言 `X-TTS-Splits ≥ 1` |
| 7 | 建議 | 報告三處需更正 | 顯存改述為取樣觀測最大值並說明基線；卸載→再請求間隔改為約 1 秒；ASR 差異描述收斂 |

## Codex 審查（第 3 輪：`docs/step2-codex-review-3.md`）

結論：**可以進入第 3 步**；第 2 輪的阻擋問題確認已修復。剩餘建議已處理：

| # | 建議 | 處理 |
|---|---|---|
| 1 | 泰文硬切的固定塊長仍可能估計超過上限（36 s） | 改為逐塊重新估計、必要時縮小切點；4,520 字泰文測例每段 ≤ 30 s |
| 2 | 解碼驗收未檢查 ffmpeg 退出碼 | 同時檢查 ffprobe／ffmpeg 退出碼與錯誤輸出 |
| 3 | 競態回歸測試沒有真正交錯；缺 janitor 測試 | 新測試在取得狀態快照後才讓工作失敗，驗證回應與快照一致（409），之後再取得結構化錯誤；另測 janitor 在另一工作執行期間清除過期工作 |
| 4 | 顯存歸因應標為推估 | 報告已改述 |

測試在 Codex 的唯讀沙箱中有 6 項因無法建立 `tmp_path` 而未執行；本機完整執行 22 項全部通過。

## 逾時評估

RTF ≈ 0.6：5 分鐘旁白約 3 分鐘、10 分鐘約 6 分鐘（另加首次載入約 6–19 s）。Codex 的 MCP 工具呼叫有逾時（預設約 60 秒），同步呼叫不足以涵蓋長旁白 → 第 3 步的 tts-mcp／CLI 以 `/v1/jobs`（工作 ID＋狀態／進度查詢）實作，避免 Agent 逾時後重送。

## 已知限制

1. `Linger=no`：user service 只在有 WSL 工作階段時執行（Agent 本身在 WSL 內，實務上不影響）。若要常駐需 `sudo loginctl enable-linger $USER`
2. 卸載後仍保留約 0.35 GB 的 CUDA context（程序常駐成本）
3. `language` 未指定時不自動偵測；第 3 步的 MCP 會要求 Agent 提供
4. 只有一個模型，模型切換以單元測試驗證
5. 工作與音訊只存在記憶體（重啟即失效，TTL 1 小時）
6. 執行中的生成無法取消（Python 執行緒無法安全中斷）；只能重啟服務
