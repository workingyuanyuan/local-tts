# 發布前稽核（2026-10-26）

推上 GitHub 前，交給不同能力的代理分頭驗證，修正後再交 Codex 複審。各代理的完整輸出不納入 repo（含本機路徑）；下面是結論與處理。

## 稽核分工

| 代理 | 範圍 | 結論 |
|---|---|---|
| Codex gpt-6.1-sol（high，唯讀） | 全部程式碼對照 CLAUDE.md 契約 | 沒有阻擋發布的缺陷；6 項應修正／建議 |
| Codex gpt-6-astra（medium，唯讀） | 完成條件、可重現性、架構風險 | 主要流程有實證；指出內容完整性、重現性與兩項架構缺口 |
| Gemini 3.8 Flash（high，唯讀） | 發布衛生：個資、大檔、授權、README 正確性 | 本機路徑、LICENSE／NOTICE、third_party 的處理方式、README 細節 |
| Gemini 4 Argon（medium，唯讀） | 實際聆聽第 2、4 步音檔並對照原文 | 正確聽出已知的 `scene03.wav` 漏唸（校準通過）；長文無整句漏唸、無截斷；有錯讀；找到一個音色切換問題 |
| Opus 子代理（live） | 對執行中的服務實測所有完成條件 | 7 項中 6 項 PASS；英文長文漏一整句、MCP 會丟掉未知參數 |

## 發現與處理

| 發現 | 來源 | 處理 |
|---|---|---|
| 英文長文漏一整句（該段只有估計的 0.66 倍），沒有觸發「過短」重試（門檻 0.3 倍） | live | 依使用者決定新增 0.7 倍「疑似漏句」門檻：重試，仍偏短時保留最長的一次，並在結果的 `warnings` 註明（不報錯）。依據：129 個已記錄段落中，正常語音最低 0.75 倍 |
| MCP 工具會靜默丟掉未知參數（如 `speed`） | live | 改為回 `unsupported_parameter`；schema 加上 `additionalProperties: false` |
| 未指定 voice 時，較短的首段（< 3 秒，例如標題）音色和後面不同 | Argon | 參考音色確定後，用它重新生成這些早期片段 |
| HTTP 本體大小在解析後才檢查 | Sol | 新增 middleware，在解析前限制 256 KiB（`body_too_large`，413） |
| 中文 model／voice 名稱放進 HTTP 標頭時拋出 `UnicodeEncodeError` | Sol | 標頭值改用 percent-encoding |
| 生成時拋出例外，錯誤沒有附段落索引 | Sol | `internal_error`／`gpu_out_of_memory` 附 `segment` |
| MCP 的工作紀錄只增不減 | Sol | 未使用 2 小時的紀錄會清除；`unknown_job` 不留紀錄 |
| `tts job --wait -o` 等待失敗時，錯誤少了 `job_id`／`fetch_command` | Sol | 補上 |
| 種子很大時，加上段落偏移後會溢位 | Sol | `seed` 限制在 0 至 2³²−1 |
| 送出後回應遺失，重送會再生成一次 | Astra | 新增 `Idempotency-Key`；客戶端用同一個鍵重試最多 2 次 |
| 完成的音訊佔用的記憶體沒有上限 | Astra | 新增 `max_result_bytes`（預設 4 GiB），超過時回 `results_full`（503） |
| README、Agent 規則、腳本寫死本機家目錄路徑 | Astra、Flash | README 改用 `$PWD`；規則原稿改用 `{{LOCAL_TTS_ROOT}}`，由安裝腳本代入；腳本改由自身位置推導路徑；`docs/` 中的連結改為相對路徑 |
| `third_party/MOSS-TTS` 是一般 clone，直接 commit 會變成空的 gitlink | Astra、Flash | 改為 submodule，固定在 commit 934d682，`uv sync --frozen` 不受影響 |
| 再發布 demo 音檔需要標示出處 | Flash | 新增 `NOTICE` |
| 報告把「逕流→除流／豬流」「過去→或許」寫成同音字或無法歸因 | Argon | 修正 `docs/step2-report.md`、`docs/step4-report.md`、`scripts/asr_compare.py` 的說法；Agent 規則加上「罕用字改用常用字形（徑流）」 |
| README 錯誤表少了 `invalid_pause` | Flash | 補上，並加入新的錯誤碼 |

## Codex 複審（第 1 輪）

複審判定「目前不宜發布」，提出 6 項應修正、2 項建議，均已修正並補上測試：
- 記憶體上限原本只在送出時檢查，存入結果時也要檢查：超出時該工作以 `results_full` 結束。
- 送出的重試會超出 MCP 呼叫的共用期限：改為依剩餘時間計算每次逾時，剩不到 1 秒就不再重試。
- 「疑似漏句」門檻多扣了 0.5 秒，0.66 倍會被放過：拿掉這段寬限（0.66、0.69 倍會重試，0.70 倍不會）。
- 早期短片段重新生成後，舊的 warning 仍留著：warning 改為跟著片段走，重新生成時一併替換。
- CLI 文字輸出沒有顯示 `warnings`：改為印到 stderr。
- `tts job -o` 用無副檔名路徑時，復原指令被補成 `.wav`：在知道工作格式前保留原路徑。
- `tts job --wait -o` 遇到工作失敗時沒有附 `output_path`：補上（仍不附 `fetch_command`）。
- `generate_speech` 遇到 `unknown_job` 時沒有清除紀錄：補上。

## Codex 複審（第 2 輪）

前 8 項中 7 項確認修正。剩下的一項：每個請求至少有 0.5 秒（`Deadline.request_timeout` 的下限），所以極短的 `wait_seconds` 仍可能超出約 0.5 秒。這個下限是刻意的，因為 `wait_seconds=0` 也必須能把工作送出。保留下限，但把保證寫明：呼叫最多比期限晚 0.5 秒結束；剩餘時間不足 1 秒時不重試（程式註解、README）。另補上「回應停滯時不會超過下限」的測試。

## Codex 複審（第 3 輪）

接受保留 0.5 秒下限，但指出「最多晚 0.5 秒」在整次呼叫層級仍不成立：`pending` 結果另做固定 2 秒的健康查詢；連續的請求也各自取得一次 0.5 秒下限。修正：
- 一次呼叫的所有請求共用「期限 + 0.5 秒」的寬限（`Deadline.request_timeout`）。
- 期限已過且已有工作快照時，不再查詢，直接回傳。
- 期限已過時跳過健康查詢，預估時間改用未知的載入狀態計算。
- CLI 的送出與等待共用同一個期限。

另補上端到端測試：健康查詢停滯、每個請求都慢、MCP 重啟後接續。

## Codex 複審（第 4 輪）

確認共用寬限有效，另提出兩項：
- `wait()` 已拿到 `succeeded`／`failed` 的快照時仍會再查詢一次；這次查詢失敗會蓋掉已知的結果。修正為已知終態時直接回傳。
- `tts say --no-wait --timeout 0` 的健康查詢沒有受期限限制。修正為一律傳入同一個期限。

兩項都補了測試。

## 未處理（記錄在案）

- **句中少幾個字的漏唸**（如 `scene03.wav` 的「計時器一響」）：依使用者先前的決定，服務端不另加防護；README「已知限制」已說明。
- **模型錯讀**（請求、閒置、處理、數字等）：屬模型能力，可用拼音或常用字形修正。
- **評估工具（uvx 執行的 faster-whisper）的間接依賴沒有鎖定**（Astra 的建議）：只影響驗收重跑，不影響服務。
- **公開完整的驗收產物**（Astra 的建議）：`outputs/` 含本機路徑、Agent 對話紀錄與數百 MB 音訊，不納入 repo；結果記錄在 `docs/`。
- 載入模型時 transformers 會警告 tokenizer 的 regex 設定；未觀察到影響（中文 CER 1.1%）。
- `/v1/jobs/{id}` 的 `result.duration` 與 CLI／MCP 輸出的 `duration_seconds` 名稱不同（皆已寫在文件中）。

## 修正後驗證

- 單元測試：96 passed（新增 28 項回歸測試，涵蓋上述每一項修正與 Codex 複審指出的邊界）。
- 第 5 輪複審判定「可發布」後，完整測試有一次 1 項失敗；之後連續 11 次都通過，失敗的是哪一項沒有記錄到。推測是新加的計時測試門檻太貼近實際值，因此改為計算請求次數，只保留寬鬆的時間上限；改完連跑 4 次，加上 3 組同時執行，都通過。
- 重啟服務後實測：
  - 超過 256 KiB 的本體回 413。
  - 同一個 `Idempotency-Key` 送兩次，拿回同一個工作。
  - 超出範圍的種子回 400。
  - 經 stdio 呼叫 MCP 並帶 `speed`，回 `unsupported_parameter`，也沒有產生檔案。
  - 英文長文（demo_en，7 段，110.2 秒）以 ASR 比對：沒有漏句，沒有未解決的差異。
- 規則重新安裝後，安裝版的路徑代入正確。
