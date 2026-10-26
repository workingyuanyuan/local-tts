# 第 3 步實測紀錄：tts-mcp 與 CLI（2026-10-21）

## 實作內容

| 模組 | 說明 |
|---|---|
| `src/local_tts/client.py` | CLI 與 MCP 共用的 **async** HTTP client（httpx.AsyncClient，每個請求在 `asyncio.timeout()` 內，逾時即取消並關閉連線）：輸出路徑解析（必須絕對路徑、必須是檔名；Windows 路徑以 `wslpath -u` 轉換；副檔名決定格式；無副檔名補上；`LOCAL_TTS_OUTPUT_DIR` 也須為絕對路徑）、覆寫保護（送出前檢查＋寫入時以 hard link 原子地不覆蓋）、同目錄暫存檔＋改名的原子寫入（權限依 umask）、`Deadline`（一次呼叫內所有請求共用的期限）、送出／輪詢／取回（下載前核對格式與副檔名）、預估剩餘時間、服務不可達／逾時的明確錯誤 |
| `src/local_tts/cli.py` | `tts say / job / models / voices / health`（async，以 `asyncio.run` 執行）；`--url`／`--json`／`-q` 可放在子命令前或後；exit code 0／1／2／3；pending 與送出後的錯誤附可直接貼上執行的取回指令（shell quoting、保留 `--url`／`--overwrite`） |
| `src/local_tts/mcp_server.py` | stdio MCP server（`mcp` 2.3.0 的 `MCPServer`）：`generate_speech`、`get_speech_job`、`list_models`、`list_voices`、`tts_status`；server instructions 說明使用時機與 pending／恢復流程；直接 await async client；每個工作一把 `asyncio.Lock`（取得鎖也受期限限制；只保存一次）；所有失敗皆為結構化 JSON 錯誤；工具 annotations（查詢為唯讀；寫入工具因 `overwrite=true` 可覆蓋檔案而標為 destructive；皆不連外） |
| `pyproject.toml` / `uv.lock` | 新增 `mcp==2.3.0` 與 entry points `tts`、`tts-mcp`；未升級或移除任何既有套件 |
| `src/local_tts/server/scheduler.py` | 工作快照新增 `format`（MCP 重啟後恢復未完成工作時用來決定副檔名） |
| `tests/test_clients.py` | 30 個測試（路徑、原子寫入與權限、CLI、MCP 工具、pending→完成、期限、送出後錯誤附 job_id、相同路徑與並行取回的冪等性、重啟後格式、io_error／`/`／相對預設目錄／非 UTF-8、取回指令 quoting、服務不可達；第 2 輪：慢速逐段回應的整體時限、無副檔名路徑的冪等、換路徑並行只保存一次、output_exists 後 overwrite 重試、恢復失敗保留 job context、CLI 寫檔失敗保留 job context、非 UTF-8 stdin；第 4 輪：慢速標頭、慢速 gzip 本體、gzip 只解碼一次；第 5 輪：下載中取消後重試只寫一次、寫檔在 event loop 執行緒；第 6 輪：`tts job -o` 寫檔失敗保留 job context）；共用 fake 移到 `tests/fakes.py`。全部測試 52 項通過 |
| `scripts/step3_run.sh`、`scripts/step3_mcp_stdio.py` | 對執行中的服務做實測，每項都斷言結果 |

### 逾時設計

每個 HTTP 請求都在 `asyncio.timeout()` 內執行：等待網路的任一階段（連線、標頭、本體）超時都會取消請求並關閉連線；取消是合作式的，兩次 await 之間的 CPU 工作不會被中斷——若伺服器無視 `Accept-Encoding: identity` 仍送 gzip，解碼不可中斷（tts-server 不壓縮，實務上不發生），寫檔在 event loop 內同步完成（毫秒級，取消不會留下背景寫入）；不使用背景執行緒（httpx 自身的 timeout 只限制單次讀取，不足以保證整體上限），一次工具呼叫的送出與輪詢共用 `wait_seconds` 期限（預設 45 秒，低於 Codex 預設的 60 秒 MCP 逾時；上限 240 秒）；期限將到時逾時的輪詢視為仍在處理並回傳 pending。只有已完成的工作才額外下載音訊（≤ 30 秒），因此一次呼叫最長約 270 秒，低於 Codex 設定的 330 秒。未完成時回傳 `status: "pending"`、`job_id`、進度、`estimated_remaining_seconds`；Agent 以 `get_speech_job(job_id)` 繼續等待。工作一旦成立，之後的任何錯誤都附 `job_id`、`output_path` 與恢復說明，避免 Agent 重送。每個工作在 MCP 程序中有一筆固定紀錄（一把鎖、格式、預設路徑與 overwrite、各路徑的保存結果）；路徑解析（以工作格式補副檔名）、覆寫選項與保存都在鎖內，因此重複或並行的取回對每個路徑只保存一次並回傳同一結果；`output_exists` 後可帶 `overwrite=true` 重試。MCP 重啟後以工作的 `format` 決定預設副檔名，指定的副檔名不符則回 `format_mismatch`。

## 註冊

| Agent | 方式 | 狀態 |
|---|---|---|
| Claude Code | `claude mcp add -s user local-tts -- …/.venv/bin/tts-mcp`（`~/.claude.json`） | `claude mcp get local-tts`：✔ Connected |
| Codex | `codex mcp add local-tts -- …/.venv/bin/tts-mcp`，並設 `tool_timeout_sec = 330` | `codex mcp get local-tts`：enabled、`tool_timeout_sec: 330` |

修改前的備份：`~/.claude.json.bak-local-tts`、`~/.codex/config.toml.bak-local-tts`。CLI 以 `~/.local/bin/tts` 連結到 `.venv/bin/tts`。

## 實測結果（`scripts/step3_run.sh outputs/step3/final --agents`，原始輸出在該目錄）

| 檢查 | 結果 |
|---|---|
| `tts health` | PASS |
| `say`（中文、demo_zh_female、mp3） | PASS |
| 目標已存在 → `output_exists`（exit 1）；另一檔案先寫再 `--overwrite` 覆寫 | PASS |
| 相對路徑 → `invalid_path`；`.ogg` → `unsupported_format` | PASS |
| 未知 voice／model → `unknown_voice`／`unknown_model`（附可用清單） | PASS |
| stdin 輸入、flac | PASS |
| `--no-wait`（exit 3、印出 job_id 與取回指令）→ `tts job --wait -o` 取回 | PASS |
| Windows 路徑 `C:\Users\…\Temp\…wav` → `/mnt/c/...` 寫入（測完刪除） | PASS |
| MCP over stdio（`mcp_stdio.json`）：短句 completed；英文長文（demo_en、mp3） | PASS：首次呼叫 20.0 s 回 pending（1/7 段，預估剩 64.7 s）；`get_speech_job` 45.0 s 後 pending（3/7 段，預估剩 43.1 s）；再 38.2 s 後 completed，音訊 109.77 s |
| Claude Code headless（`agent_claude.jsonl`，stream-json 完整事件）：`claude -p "Use the local-tts MCP tool generate_speech …" --allowedTools mcp__local-tts__generate_speech` | PASS：exit 0；事件中有 `mcp__local-tts__generate_speech` 的 tool_use 及 `status: completed` 的 tool_result（之前先以 ToolSearch 載入延遲載入的 MCP 工具）；寫出 `agent_claude.wav` |
| Codex headless（`agent_codex.log`）：`codex exec -c 'mcp_servers.local-tts.default_tools_approval_mode="approve"' …`（單次覆寫，未寫入設定） | PASS：exit 0；log 中有 `mcp: local-tts/generate_speech (completed)`；寫出 `agent_codex.wav` |
| 8 個預期輸出逐一斷言：存在、48000 Hz、2 聲道、ffmpeg 完整解碼（exit 0 且無錯誤輸出）、權限 644 | PASS（缺檔亦判 FAIL） |

未加核准覆寫時，Codex headless 會回 `MCP tool call requires approval, but approval policy is never`（第一輪實測觀察，見「待你決定」）。以上只驗證「工具註冊可被呼叫」；「在需要旁白的任務中自行選用」屬第 4 步（使用規則）的驗收。

## 遇到的問題

1. `mcp` 2.x 將 FastMCP 改名為 `MCPServer`（`mcp.server.mcpserver`），`ToolAnnotations` 欄位改為 snake_case → 依 2.3.0 實際 API 撰寫
2. SDK 會在 tool error 前加 `Error executing tool X: ` 前綴，其後仍是完整 JSON 錯誤 → 可接受，測試以第一個 `{` 起解析
3. `mkstemp` 建立的檔案為 0600，改名後輸出檔對其他程式不可讀 → 寫入後依 umask 設為 0644
4. 預估剩餘時間偏低（克隆聲音的音訊比文字估計長）→ 估算 RTF 由 0.65 調為 0.75
5. MCP 程序的 httpx INFO 日誌每次輪詢一行 → 降為 WARNING
6. `codex exec` 未提供 stdin 時會等待輸入 → 以 `< /dev/null` 執行
7. Codex 的 MCP 工具預設需人工核准（見上）
8. 我在測試中三度用 `pkill -f <pattern>` 時比對到自己的 shell 而中斷指令；之後改以 PID 停止程序

## Codex 審查與修正（第 1 輪：`docs/step3-codex-review-1.md`）

| # | 嚴重度 | 問題 | 修正 |
|---|---|---|---|
| 1 | 阻擋 | `wait_seconds` 只限制輪詢；送出、health、下載另計，可能超過 MCP 逾時 | `Deadline` 由送出與輪詢共用，每個請求的 HTTP 逾時不超過剩餘時間；期限將到時的輪詢逾時回 pending；health 查詢 2 s；下載 30 s；上限 240 s。測試：0.5 s 期限的呼叫 < 3 s 返回 |
| 2 | 阻擋 | 工作成立後的通訊錯誤遺失恢復資訊 | MCP：附 `job_id`、`output_path`、`recovery`；CLI：附 `job_id`、`output_path`、`fetch_command`。測試涵蓋 |
| 3 | 阻擋 | 帶 `output_path` 的重複取回會重建紀錄而報 `output_exists`；並行取回無鎖 | 同一路徑沿用既有紀錄；每工作一把鎖、只保存一次，其餘等待者回傳同一結果或 pending。測試涵蓋（3 個並行呼叫） |
| 4 | 阻擋 | MCP 重啟後恢復未完成工作，可能把 FLAC 寫到 `.wav` | 伺服器快照提供 `format`；恢復時以它決定副檔名；下載前核對副檔名與實際格式。測試涵蓋 |
| 5 | 阻擋 | `OSError`、輸出 `/`、非 UTF-8 檔案沒有結構化錯誤 | `io_error`（含 errno、檔名）、`invalid_path`、`invalid_input`；其他例外為 `internal_error`。測試涵蓋 |
| 6 | 阻擋 | 相對的 `LOCAL_TTS_OUTPUT_DIR` 繞過絕對路徑檢查 | 使用前檢查，否則 `invalid_path`。測試涵蓋 |
| 7 | 建議 | CLI 取回指令未 quote、遺漏 `--url`／`--overwrite` | `shlex.join`，保留兩個選項。測試涵蓋含空白的路徑 |
| 8 | 建議 | 寫入工具標為非 destructive，與 `overwrite` 不符 | 改為 `destructive_hint=True`；報告權限說明更正 |
| 9 | 建議 | 報告數字、驗收強度與證據保存 | 數字依本次原始輸出；腳本斷言 48 kHz 立體聲；覆寫測試不再覆蓋克隆產物；Agent 呼叫紀錄保存；腳本中 Agent 結果改為不在 pipeline 內判定（原寫法在 subshell 設定 `FAIL` 無效） |

## Codex 審查與修正（第 2 輪：`docs/step3-codex-review-2.md`）

| # | 嚴重度 | 問題 | 修正 |
|---|---|---|---|
| 1 | 阻擋 | httpx timeout 只限單次讀取，逐段慢速回應可超過期限 | 第一版改用執行緒池＋`future.result(timeout)`，但引入背景請求問題（見第 3 輪）；之後改為串流逐區塊檢查（第 4 輪指出仍不完整）；最終為 async client（見第 4、5 輪） |
| 2 | 阻擋 | 無副檔名路徑先以 `.wav` 比較，重複取回失去冪等 | 路徑一律以工作格式解析後再比較；測試涵蓋 FLAC |
| 3 | 阻擋 | 換路徑會重建鎖，並行保存兩次 | 每工作固定一筆紀錄與鎖；路徑判斷、保存結果（per path）都在鎖內；測試以 3 個並行呼叫取回到新路徑 |
| 4 | 阻擋 | `output_exists` 後 `overwrite=true` 重試仍沿用舊值 | 覆寫選項在鎖內依本次呼叫決定（省略路徑時與原設定取 OR）；測試涵蓋 |
| 5 | 阻擋 | CLI 寫檔 `OSError`、MCP 恢復時的前置查詢失敗遺失 job context | CLI 捕捉送出後所有例外並附 `job_id`／`output_path`／`fetch_command`；MCP 前置查詢移入 `_deliver`，錯誤一律附 `job_id`／`recovery`；測試涵蓋 |
| 6 | 建議 | 非 UTF-8 stdin 未處理 | stdin 以 bytes 讀取並嚴格解碼，命令列文字亦檢查 → `invalid_input`；測試涵蓋 |
| 7 | 建議 | 實測腳本可能誤判 PASS；Claude 呼叫證據不完整 | Agent 驗收同時要求 exit 0、工具呼叫證據（Claude stream-json 的 tool_use＋completed tool_result；Codex log 的 completed 事件）與音檔；預期檔案逐一檢查（缺檔即 FAIL）、精確比對取樣率與聲道、斷言權限 644 |

## Codex 審查與修正（第 3 輪：`docs/step3-codex-review-3.md`）

| # | 嚴重度 | 問題 | 修正 |
|---|---|---|---|
| 1 | 阻擋 | 執行緒池版本只停止等待：逾時請求仍在背景執行；池滿時 `submit` 先回逾時、之後才送出 POST（產生無人接收的工作）；背景請求拖延 CLI 退出 | 移除執行緒池，先改為串流逐區塊檢查（第 4 輪指出仍不完整），最終改為 async client（見第 4 輪） |
| 2 | 建議 | 腳本 Windows 清理以 `xargs` 拆路徑；stdin 測試把路徑嵌入 `bash -c` 字串 | 以 Python 刪除；以位置參數傳入路徑 |
| 3 | 建議 | 報告稱共同選項可放任何位置，但 `tts --json health` 失敗 | 頂層與子命令都接受 `--url`／`--json`／`-q`（子命令以 SUPPRESS 預設，不覆蓋頂層值） |
| 4 | 備註 | `agent_claude.json` 是舊產物 | 腳本開始時清除舊的 `agent_*`；本次證據為 `agent_claude.jsonl` |

## Codex 審查與修正（第 4 輪：`docs/step3-codex-review-4.md`）

| # | 嚴重度 | 問題 | 修正 |
|---|---|---|---|
| 1 | 阻擋 | 串流截止只在 `iter_bytes()` 產出資料後檢查：慢速標頭、gzip 尚未輸出時不受限（實測 0.5 s 期限約 3.2 s 才截止） | client 改為 async（`httpx.AsyncClient`），每個請求包在 `asyncio.timeout()`，等待網路的任何階段逾時都會取消並關閉連線；MCP 直接 await、CLI 以 `asyncio.run`。測試以真實 socket 每 80 ms 送 1 byte，分別在本體、標頭、gzip 本體三種階段驗證：0.5 s 期限於 < 0.8 s 內回 `server_timeout`、伺服器端在 1.5 s 內察覺連線關閉、無殘留執行緒 |
| 2 | 建議 | 重建 Response 時保留 `Content-Encoding: gzip`，造成二次解碼 | 不再重建 Response；請求帶 `Accept-Encoding: identity`；測試合法 gzip 回應只解碼一次 |
| 3 | 建議 | MCP 實測失敗時保留上輪 `mcp_stdio.json` | 腳本開始時清除；`step3_mcp_stdio.py` 在 `finally` 寫出本輪紀錄（含失敗原因） |

## Codex 審查與修正（第 5 輪：`docs/step3-codex-review-5.md`）

| # | 嚴重度 | 問題 | 修正 |
|---|---|---|---|
| 1 | 阻擋 | `save` 以 `asyncio.to_thread` 寫檔：呼叫被取消時執行緒仍在寫、鎖已釋放、快取未更新，立即重試會出現兩個並行寫入者 | 寫檔改在 event loop 內同步執行（本機寫入數 MB 為毫秒級；同步段不可被取消，寫檔、快取更新與釋放鎖因此一致）。測試：下載中取消後重試只寫一次、鎖已釋放；並斷言寫檔在 event loop 執行緒（防止日後改回 `to_thread`） |
| 2 | 建議 | `asyncio.timeout` 為合作式取消，無法中斷同步解碼；第 2 輪對照表敘述過時 | 敘述改為「等待網路的階段」可取消，並註明合作式取消的限制（伺服器仍送 gzip 時的解碼、同步寫檔）；第 2 輪對照表改述最終方案 |

## Codex 審查（第 6 輪：`docs/step3-codex-review-6.md`）

結論：**可以進入第 4 步**；第 5 輪的阻擋問題確認已修正，無新的阻擋問題。剩餘兩項建議已處理：期限敘述補上合作式取消的限制（報告、README、程式註解）；`tts job -o` 寫檔失敗也附 `job_id`／`output_path`／`fetch_command`（新增測試）。測試在 Codex 的唯讀沙箱中因無法建立 `tmp_path` 與 socket 有部分未執行；本機完整執行全部通過。

## 待你決定

- **Codex 免核准**：要讓 Codex 在任務中自行呼叫（含 `codex exec` 這類非互動執行），需在 `~/.codex/config.toml` 的 `[mcp_servers.local-tts]` 加 `default_tools_approval_mode = "approve"`（可選值 `auto`／`prompt`／`writes`／`approve`）。互動模式下不設定也能用，只是每次會詢問。
- **Claude Code 免詢問**：互動模式下第一次呼叫會詢問權限；若要免詢問，可在 `~/.claude/settings.json` 的 `permissions.allow` 加入 `mcp__local-tts`。

這兩項都會放寬 Agent 的權限：工具會在 Agent 指定的路徑建立音檔，`overwrite=true` 時可覆蓋該路徑的既有檔案，不連外網。所以沒有自行設定。
