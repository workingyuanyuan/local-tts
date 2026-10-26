# local-tts

本地 TTS 服務，讓 Claude Code、Codex 等 Agent 依任務需求生成旁白語音。第一版模型：MOSS-TTS-Local-Transformer-v1.5。

架構與設計約束見 [CLAUDE.md](CLAUDE.md)；實測紀錄：[第 1 步](docs/step1-report.md)、[第 2 步](docs/step2-report.md)、[第 3 步](docs/step3-report.md)、[第 4 步](docs/step4-report.md)、[發布前稽核](docs/final-audit.md)。

## 安裝

需求：WSL2 Ubuntu（systemd 啟用）、NVIDIA 驅動、`ffmpeg`（`sudo apt-get install ffmpeg`）、[uv](https://docs.astral.sh/uv/)。

```bash
git clone --recurse-submodules <本 repo 的 URL> ~/Projects/local-tts   # MOSS-TTS 是固定在 934d682 的 submodule
cd ~/Projects/local-tts
UV_HTTP_TIMEOUT=600 uv sync --frozen        # 依 uv.lock 建立 .venv（Python 3.12）
# 下載固定 revision 的權重到 ~/.cache/huggingface/（約 18 GB）
uvx --from huggingface_hub hf download OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5 --revision be7766a6735b98bd793f7c79fb720b4d0f5d13b8
uvx --from huggingface_hub hf download OpenMOSS-Team/MOSS-Audio-Tokenizer-v2 --revision f6e20e543b33d2c252a7ef71bdf8aa71e5ff9169
```

已 clone 但沒有 submodule 時補跑 `git submodule update --init third_party/MOSS-TTS`（不需要 MOSS-TTS 自己的 submodule）。`deploy/local-tts.service` 假設專案位於 `~/Projects/local-tts`；放在其他位置時改 service 檔的 `WorkingDirectory` 與 `ExecStart`。

套件來源由 `pyproject.toml` 固定：torch/torchaudio 來自 PyTorch cu128 index；torchcodec 用 PyPI 的 CPU 版（cu128 版需要 NVIDIA NPP，而我們只處理音訊）。

## 啟動／停止（systemd user service）

```bash
mkdir -p ~/.config/systemd/user
cp deploy/local-tts.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now local-tts     # 啟動並設定登入後自動啟動
systemctl --user restart local-tts          # 重啟（改了 config/*.yaml 後需要）
systemctl --user stop local-tts             # 停止
systemctl --user status local-tts
```

- 日誌：`journalctl --user -u local-tts -f`
- 健康檢查：`curl -s localhost:8765/health`
- 服務只在 WSL 執行期間運作；模型延遲載入（第一個請求才載入，約 10–20 秒），閒置 10 分鐘後自動卸載。
- 前景執行（除錯）：`HF_HUB_OFFLINE=1 .venv/bin/tts-server`

## CLI：`tts`

`~/.local/bin/tts` 連結到 `.venv/bin/tts`（在專案根目錄執行 `ln -sfn "$PWD/.venv/bin/tts" ~/.local/bin/tts`）。

```bash
tts say "你好，歡迎收看。" -l zh -o /abs/path/hello.wav            # 等到完成，印出路徑與時長
tts say -f script.txt -l zh -v demo_zh_female -o /abs/out.mp3     # 從檔案讀（-f - 讀 stdin）
tts say "..." -o /abs/out.wav --no-wait --json                    # 只送出，exit 3 並印出 job_id
tts job <job_id> --wait -o /abs/out.wav                           # 等待並取回
tts models | tts voices [-m MODEL] | tts health
```

- `-o` 必須是絕對路徑，Windows 路徑（`C:\...`）會用 `wslpath` 轉換；副檔名決定格式（.wav／.flac／.mp3）
- 目標已存在時報錯，加 `--overwrite` 才覆寫；未指定 `-o` 時寫到 `~/.local/share/local-tts/output/<timestamp>.wav`
- `--json` 輸出機器可讀結果；錯誤一律以 JSON 印到 stderr；結果有 `warnings`（可能漏句）時，文字模式會印到 stderr
- exit code：0 完成、1 錯誤、2 用法錯誤、3 仍在處理（`--no-wait` 或 `--timeout` 到期，工作會繼續執行）
- 工作送出後的錯誤附 `job_id`、`output_path` 與 `fetch_command`（接續等待用）；工作本身失敗（`job_status: "failed"`）或服務已不認得該工作（`unknown_job`，服務重啟或過期）時不附 `fetch_command`，需修正後重新送出（`tts say` 與 `tts job` 皆同）
- 環境變數：`LOCAL_TTS_URL`（服務位址）、`LOCAL_TTS_OUTPUT_DIR`（預設輸出目錄）

## MCP：`tts-mcp`

stdio MCP server（`.venv/bin/tts-mcp`）。工具：

| 工具 | 說明 |
|---|---|
| `generate_speech` | `text`、`output_path`（絕對路徑）、`language`、`voice`、`model`、`format`、`overwrite`、`seed`、`wait_seconds`。完成時回傳 path、duration_seconds、sample_rate、channels、format、model、voice；有段落偏短（可能漏句）時另附 `warnings` |
| `get_speech_job` | 等待 `pending` 的工作並寫檔；重複呼叫回傳同一結果 |
| `list_models` / `list_voices` / `tts_status` | 唯讀查詢 |

一次呼叫的送出與等待共用 `wait_seconds` 期限（預設 45 秒、上限 240 秒；每個 HTTP 請求都在 `asyncio.timeout()` 內，等待網路時逾時即取消；整次呼叫的請求共用期限後 0.5 秒的寬限，讓 `wait_seconds=0` 也能送出，所以網路等待最多比期限晚 0.5 秒結束；期限已過時不再查詢狀態，也不查詢健康狀態，直接回 `pending`；送出沒收到回應時，剩餘時間不足 1 秒就不再重試），完成時另加音訊下載（≤ 30 秒）與本機寫檔（同步、毫秒級；取消為合作式，不會中斷這段）；長旁白會先回 `status: "pending"` 與 `job_id`、進度與預估剩餘秒數，再用 `get_speech_job` 接著等，不會重送。錯誤以 tool error 回傳 JSON（`{"error": {"code", "message", ...}}`）；`pending` 結果附 `output_path` 與 `next`（`{"tool": "get_speech_job", "arguments": {job_id, output_path, overwrite?}}`），照 `next.arguments` 呼叫即可接續，MCP 重啟後也一樣；明確傳回 `generate_speech` 的同一路徑時沿用其 `overwrite` 設定。工作成立後的錯誤附 `job_id`、`output_path` 與 `recovery`：呼叫中斷（連線、逾時、寫檔）時給出 `get_speech_job` 的參數（JSON）；工作本身失敗（`job_status: "failed"`）或 `unknown_job` 時指示修正後重新 `generate_speech`。未給路徑且 MCP 已重啟時，寫到預設輸出目錄（格式沿用工作格式）。

註冊（在專案根目錄執行）：

```bash
claude mcp add -s user local-tts -- "$PWD/.venv/bin/tts-mcp"
codex mcp add local-tts -- "$PWD/.venv/bin/tts-mcp"
```

Codex 另在 `~/.codex/config.toml` 的 `[mcp_servers.local-tts]` 設 `tool_timeout_sec = 330`（大於一次呼叫的最長時間約 270 秒）與 `default_tools_approval_mode = "approve"`（免核准；未設時每次呼叫都要核准，headless `codex exec` 會直接拒絕）。Claude Code 在 `~/.claude/settings.json` 的 `permissions.allow` 加入 `mcp__local-tts`（未設時第一次呼叫會詢問）。兩者開放後，Agent 可不經詢問在它指定的路徑建立音檔（帶 `overwrite` 時可覆寫）；工具不連外網。

## Agent 使用規則

規則原稿在 `agent-rules/`，用 `scripts/install_agent_rules.sh` 安裝（可重複執行）：

| Agent | 安裝位置 |
|---|---|
| Claude Code | `~/.claude/skills/narration/SKILL.md`（user-level skill，依 description 自動觸發） |
| Codex | `~/.codex/AGENTS.md` 中 `<!-- local-tts:begin -->`…`<!-- local-tts:end -->` 之間的區塊（其餘內容不動；第一次安裝前備份為 `AGENTS.md.bak-local-tts`） |

規則內容：觸發情境（影片旁白、簡報配音、教材朗讀、有聲內容；只要文字稿時不觸發）、為耳朵寫稿（數字讀法、`[pause]`、拼音修正讀音）、模型與聲音選擇（未指定 `voice` 時每次呼叫音色不同，多段需同一旁白者時固定同一 `voice`）、輸出放在任務專案目錄、依用途切分檔案（每個場景／投影片一檔）、`pending` 時用 `get_speech_job` 接續、交給 ffmpeg 合成的方式與錯誤處理。修改規則後重新執行安裝腳本。

## 設定

| 檔案 | 內容 |
|---|---|
| `config/server.yaml` | 監聽位址／埠（預設 `127.0.0.1:8765`）、佇列上限、閒置卸載秒數、工作保留時間、同步等待上限、切段參數 |
| `config/models.yaml` | 模型註冊表（`default`、adapter、HF revision、codec 精度、取樣參數） |
| `config/voices.yaml` | 聲音註冊表（名稱、描述、語言、參考音檔、相容模型） |

環境變數：`LOCAL_TTS_CONFIG_DIR`（改用其他設定目錄）、`LOCAL_TTS_PORT`。

新增聲音：把參考音檔（幾秒鐘清楚的單人語音，wav/mp3/flac 皆可）放到 `assets/voices/`，在 `voices.yaml` 加一筆，重啟服務。

## HTTP API

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/health` | 服務狀態、已載入模型、佇列長度、執行中工作、GPU 記憶體 |
| GET | `/v1/models` | 模型清單：id、是否預設、是否已載入、支援語言、能力、可用選項 |
| GET | `/v1/voices?model=` | 聲音清單：name、描述、語言、相容模型 |
| POST | `/v1/audio/speech` | 同步生成，直接回傳音訊 bytes（OpenAI 相容子集） |
| POST | `/v1/jobs` | 非同步生成，回傳 202 與工作 ID（長旁白用，避免呼叫端逾時） |
| GET | `/v1/jobs/{id}` | 工作狀態：queued／running／succeeded／failed、佇列位置、段落進度、結果中繼資料或錯誤 |
| GET | `/v1/jobs/{id}/audio` | 取得完成工作的音訊 |

請求本體（`/v1/audio/speech` 與 `/v1/jobs` 相同）：

| 欄位 | 必填 | 說明 |
|---|---|---|
| `input` | ✔ | 要朗讀的文字（上限 20000 字元）。支援 `[pause 1.5s]` 停頓標記；空行視為段落 |
| `model` | | 省略時用 `models.yaml` 的 `default` |
| `voice` | | 省略時不克隆，用模型預設音色 |
| `response_format` | | `wav`（預設，16-bit PCM）、`flac`、`mp3` |
| `language` | | **擴充欄位**：語言代碼或名稱（`zh`、`en`、`Chinese`…）；已知語言時建議提供 |
| `seed` | | **擴充欄位**：整數亂數種子（0 至 2³²−1） |

其他欄位（如 OpenAI 的 `speed`、`instructions`）一律回 `unsupported_parameter`，不會被靜默忽略。請求本體上限 256 KiB（解析前檢查）。

`POST /v1/jobs` 與 `/v1/audio/speech` 接受 `Idempotency-Key` 標頭（1–128 個可列印 ASCII 字元）：用同一個鍵重送，會拿回第一次建立的工作，不會再生成一次。`tts` CLI 與 `tts-mcp` 每次送出都帶新鍵；送出後沒收到回應（連線中斷、逾時）時，會用同一個鍵重試最多 2 次。

音訊回應標頭（`X-TTS-Model`、`X-TTS-Voice`、`X-TTS-Language` 以 percent-encoding 編碼，名稱可含中文）：`X-TTS-Job-Id`、`X-TTS-Model`、`X-TTS-Voice`、`X-TTS-Duration`、`X-TTS-Sample-Rate`、`X-TTS-Channels`、`X-TTS-Segments`、`X-TTS-Language`、`X-TTS-Retries`、`X-TTS-Splits`（截斷切分次數）。輸出為 48 kHz 立體聲。

```bash
curl -s localhost:8765/v1/audio/speech -H 'Content-Type: application/json' \
  -d '{"input":"你好，歡迎收看。","language":"zh","voice":"demo_zh_female"}' -o hello.wav
```

### 錯誤

錯誤一律為 `{"error": {"code": ..., "message": ..., ...}}`：

| code | HTTP | 說明 |
|---|---|---|
| `unknown_model` / `unknown_voice` | 404 | 附 `available` 列出可用項目 |
| `incompatible_voice` | 400 | 聲音不支援該模型 |
| `unsupported_parameter` / `unsupported_format` / `unsupported_language` | 400 | 附可用清單 |
| `empty_input` / `input_too_long` / `invalid_pause` / `invalid_request` | 400 | `invalid_pause`：單一 `[pause]` > 60 秒或總計 > 600 秒 |
| `body_too_large` | 413 | 請求本體超過 256 KiB |
| `queue_full` | 503 | 佇列已滿（含 `Retry-After`）；稍後重試 |
| `results_full` | 503 | 尚未過期的完成音訊已佔用 `max_result_bytes`（預設 4 GiB）記憶體（含 `Retry-After`）；稍後重試 |
| `sync_timeout` | 504 | `/v1/audio/speech` 超過 `sync_timeout_seconds`（預設 1800 秒）仍未完成；附 `job_id`，工作繼續執行，改用 `/v1/jobs/{id}` 查詢，不要重送 |
| `segment_truncated` | 500 | 某段切細重試後仍被截斷；附 `segment`（段落索引）與 `text` |
| `segment_failed` | 500 | 某段重試後輸出仍異常（過短／空白）；附 `segment` 與 `text` |
| `model_load_failed` / `gpu_out_of_memory` / `internal_error` | 500 | 生成時發生的例外附 `segment`（段落索引） |
| `unknown_job` / `job_not_finished` | 404 / 409 | `job_not_finished` 附 `job_status`（queued／running） |

## 長文處理

1. 依句界（`。！？；…`、英文句點）切句，`[...]` 標記不拆；過長的句子再依逗號切
2. 依估計時長把句子打包成段（預設每段 ≤ 30 秒）
3. 每段生成上限依估計時長設定（≤ `max_new_tokens`）；達上限視為截斷 → 切成兩半重試（最多 2 層；子段同樣處理邊界 `[pause]` 與參考音色）
4. 輸出比估計短太多（< 0.3× 估計 − 0.5 秒，視為漏唸）或長太多（> 2.2× 估計 + 3 秒，視為模型唸完後繼續發聲）→ 重試（最多 2 次）；最終失敗回報段落索引（`segment_failed`）。介於 0.3× 與 0.7× 之間（可能漏了一句；實測漏一整句的英文段為 0.66×，129 個正常段落最低 0.75×）也重試，重試後仍偏短時保留最長的一次，不報錯，但在結果的 `warnings` 註明是哪一段。實測正常語音多為估計的 0.75–2.1 倍（中文約 1.5 倍）
5. 去除每段首尾靜音後拼接：段間 0.35 秒、段落間 0.8 秒；位於段首尾的 `[pause]` 轉為實際靜音
6. 有 `voice` 時每段用同一參考音檔；未指定時以第一段（≥ 3 秒）的輸出作為後續段的參考，維持音色一致；在它之前的較短片段（例如標題）會用該參考重新生成一次

## 已知限制

- **模型錯讀與偶發漏字**：整句漏唸通常會讓該段明顯偏短而觸發重試（見「長文處理」第 4 點）；MOSS-TTS 偶爾會讀錯字（實測如「請求」「閒置」「過去」、罕用字「逕流」），也可能漏掉片段開頭幾個字（第 4 步 `scene03.wav` 漏了「計時器一響」）。句中少幾個字的漏唸是模型本身的問題，服務端不另加防護（時長檢查抓不到）。需要完整性保證時，用 `scripts/asr_compare.py` 回轉比對並人工聆聽；讀音可用拼音或常用字形修正（見 Agent 規則）。
- **未指定 `voice` 的音色**：同一次呼叫內維持一致，不同呼叫之間音色不同；多段需同一旁白者時請固定 `voice`。
- 服務只綁定 `127.0.0.1`，沒有身分驗證；不要對外開放埠。

## 測試

```bash
.venv/bin/python -m pytest -q tests          # 切段、重試／截斷、排程與 API（假 adapter，不需 GPU）
```

第 4 步驗收（服務需在執行中，約 20 分鐘）：`scripts/step4_all.sh <工作目錄> outputs/step4/<名稱>`。先讓 Claude Code 與 Codex 的正例同時執行（共用 GPU 佇列），再單獨依序跑反例；單一 Agent 或部分任務用 `scripts/step4_run.sh <工作目錄> <輸出目錄> [任務...]`，但反例必須在沒有其他工作時執行。以 headless 模式交給兩個 Agent 實際任務（prompt 不提到 TTS 工具）：製作有旁白的短片、把教材做成可聽的版本，以及只要文字稿的反例。斷言在 `scripts/step4_check.py`：
- 正例：有工具呼叫與完成結果，音檔位於任務目錄且可解碼；
- 影片：20–30 秒，涵蓋所有片段，場景切點對齊各段旁白；
- 反例：沒有任何工具呼叫或 CLI／HTTP 存取，服務日誌也沒有工作入列；
- 每段音檔與成品影片都以 ASR 和 Agent 送出的文字比對。

工作目錄需在本專案之外（避免讀到本專案的 CLAUDE.md），且須為新目錄或此腳本建立過的目錄。成品與 ASR 都以 `<輸出目錄>` 下經雜湊驗證的副本為準；`STEP4_RECHECK=1` 只重跑檢查，但需要原工作目錄仍在（放在 `/tmp` 時，WSL 重啟後就會消失）。ASR 未解決的項目需要有人聆聽，並記錄在 `<輸出目錄>/<agent>/asr_review.tsv`；只有結論為 `ok` 才會放行。

ASR 比對（`scripts/asr_compare.py`）會把每個漏字、多字的地方剪出來重新轉寫確認，以排除 Whisper 自身的跳字與幻覺。驗證比對本身：`scripts/asr_validation.sh outputs/step4/asr_validation`（已知缺陷必須判 FAIL、正常音檔必須判 PASS）。驗證集與所有實測產物都在 `outputs/`，因內含本機路徑與大型音訊而不納入 repo；報告（`docs/`）記錄了結果。

第 3 步實測（服務需在執行中）：`scripts/step3_run.sh outputs/step3/<名稱> [--agents]`，涵蓋 CLI 各種成功／錯誤情境、Windows 路徑、`--no-wait` 後取回，以及經 stdio 驅動 `tts-mcp` 的長文 pending 流程。

第 2 步完整實測（會暫停 systemd 服務、跑完再啟動；約 25 分鐘）：`scripts/step2_run.sh outputs/step2/<名稱>`，包含佇列／FIFO、長文、閒置卸載、強制截斷切分、ASR 回轉比對（`scripts/asr_compare.py`）與說話人一致性（`scripts/speaker_consistency.py`），原始紀錄全部存在輸出目錄。
