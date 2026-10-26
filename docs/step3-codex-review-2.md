**需先修正。** 修正對照表第 1、2、3 項仍有可重現缺陷，第 9 項的驗收與證據保存也尚未完整。

1. **阻擋：`Deadline` 仍無法限制整體請求時間。**  
   傳給 httpx 的 timeout 限制每次讀取的等待時間，持續緩慢回傳資料仍可超過期限。保留真實 HTTPTransport／httpcore，僅以記憶體 socket 每 0.08 秒提供少量資料，實測 `Deadline(0.5)` 耗 **1.449 秒**才返回。下載的 30 秒亦非整體上限，因此報告與 README 的「最長約 270 秒」尚無保證。現有期限測試使用立即回應的 HTTP，未涵蓋此情境。  
   證據：[client.py:63](../src/local_tts/client.py#L63)、[client.py:200](../src/local_tts/client.py#L200)、[client.py:221](../src/local_tts/client.py#L221)、[test_clients.py:165](../tests/test_clients.py#L165)、[README.md:77](../README.md#L77)。

2. **阻擋：無副檔名路徑的取回仍不具冪等性。**  
   FLAC 工作連續兩次以相同 `output_path="/tmp/voice"` 取回，第一次完成並保存 `/tmp/voice.flac`，第二次報 `output_exists`。原因是比較既有路徑時先用 `fmt=None` 補成 `.wav`，因此重建紀錄、丟失完成結果。MP3 同樣受影響。應依工作格式解析後再比較。  
   證據：[mcp_server.py:117](../src/local_tts/mcp_server.py#L117)、[mcp_server.py:121](../src/local_tts/mcp_server.py#L121)。現有測試只使用明確的 `.wav`：[test_clients.py:182](../tests/test_clients.py#L182)。

3. **阻擋：更換路徑會重建鎖，並行保存問題仍存在。**  
   已有路徑 A 時，兩個並行取回皆指定 B，會各自建立 `PendingJob` 與鎖。記憶體重現確認 `save(B, False)` 執行兩次，一個成功、另一個報 `output_exists`。鎖須穩定綁定工作，路徑判斷與更新也須在其保護範圍內。  
   證據：[mcp_server.py:114](../src/local_tts/mcp_server.py#L114)、[mcp_server.py:122](../src/local_tts/mcp_server.py#L122)、[mcp_server.py:124](../src/local_tts/mcp_server.py#L124)。現有並行測試未傳入新路徑：[test_clients.py:191](../tests/test_clients.py#L191)。

4. **阻擋：保存失敗後，同一路徑的 `overwrite=true` 重試無效。**  
   若首次保存因目標檔案出現而報 `output_exists`，再指定相同路徑及 `overwrite=true`，程式沿用舊 entry 的 `overwrite=False`，仍然失敗。已以假 client 核實實際傳入 `save` 的參數。應在鎖內處理重試所提供的覆寫選項。  
   證據：[mcp_server.py:118](../src/local_tts/mcp_server.py#L118)、[mcp_server.py:89](../src/local_tts/mcp_server.py#L89)、[mcp_server.py:191](../src/local_tts/mcp_server.py#L191)。

5. **阻擋：錯誤恢復資訊仍有遺漏。**  
   CLI 的 `save` 拋出 `PermissionError` 時，回覆只有 `io_error`、message，缺少已知的 `job_id`、`output_path`、`fetch_command`；補資訊的區塊只捕捉 `ClientError`。MCP 重啟恢復時，前置 `client.job` 或路徑解析失敗也位於附加 context 的 try 外，會遺失 `job_id` 與恢復說明。兩者均已記憶體重現，與報告「工作成立後的任何錯誤」承諾不符。  
   證據：[cli.py:79](../src/local_tts/cli.py#L79)、[cli.py:167](../src/local_tts/cli.py#L167)、[mcp_server.py:120](../src/local_tts/mcp_server.py#L120)、[mcp_server.py:126](../src/local_tts/mcp_server.py#L126)、[step3-report.md:17](../docs/step3-report.md#L17)。

6. **建議：非 UTF-8 stdin 仍可能產生未捕捉例外。**  
   文字檔分支已有處理，但 stdin 分支未統一驗證。以 `surrogateescape` stdin 輸入非法 UTF-8 bytes，實測在編碼請求 JSON 時直接拋出 `UnicodeEncodeError`。應同樣回傳 `invalid_input`。  
   證據：[cli.py:57](../src/local_tts/cli.py#L57)、[cli.py:60](../src/local_tts/cli.py#L60)、[cli.py:164](../src/local_tts/cli.py#L164)。

7. **建議：實測腳本仍可能誤判 PASS，Claude 呼叫證據也不完整。**  
   Agent 驗收只判斷音檔非空，未檢查程序退出碼或完成結果；音檔檢查只遍歷現存檔案，缺檔會被略過；`*,48000,2*` 並非精確聲道判斷；權限僅列印。因此報告「所有輸出斷言……權限 644」敘述過強。Claude JSON 保存最終結果與 turns，尚缺實際工具呼叫事件；Codex log 已保存該事件。  
   證據：[step3_run.sh:54](../scripts/step3_run.sh#L54)、[step3_run.sh:61](../scripts/step3_run.sh#L61)、[step3_run.sh:70](../scripts/step3_run.sh#L70)、[step3_run.sh:73](../scripts/step3_run.sh#L73)、[step3-report.md:43](../docs/step3-report.md#L43)、[agent_claude.json:1](../outputs/step3/final/agent_claude.json#L1)、[agent_codex.log:15](../outputs/step3/final/agent_codex.log#L15)。

8. **備註：其餘修正與本次產物已有支持證據。**  
   快照提供 `format`、下載前核對格式、`/` 與相對預設目錄驗證、非 UTF-8 文字檔錯誤、取回指令 quoting／參數保留，以及 destructive annotations，均有對應實作。長文 **20.0／45.0／42.1 秒、111.20 秒音訊**與原始輸出一致；保留的 8 個音檔均完整解碼成功，為 48 kHz、雙聲道、0644。  
   證據：[scheduler.py:100](../src/local_tts/server/scheduler.py#L100)、[client.py:218](../src/local_tts/client.py#L218)、[client.py:73](../src/local_tts/client.py#L73)、[client.py:86](../src/local_tts/client.py#L86)、[cli.py:44](../src/local_tts/cli.py#L44)、[cli.py:58](../src/local_tts/cli.py#L58)、[mcp_server.py:37](../src/local_tts/mcp_server.py#L37)、[mcp_stdio.json:29](../outputs/step3/final/mcp_stdio.json#L29)、[summary.txt:20](../outputs/step3/final/summary.txt#L20)。

9. **備註：本次測試結果為 16 passed、24 setup errors。**  
   指定命令在 pytest 輸出擷取初始化時，因無可用暫存目錄而中止。改用記憶體擷取並處理沙箱事件迴圈喚醒限制後，取得上述結果。未進入測試本體的是 `test_clients.py` 除 `test_mcp_tools_listed`、`test_server_unreachable_is_reported` 外的 **16 項**，以及 `test_server.py` 全部 **8 項**，均因 `tmp_path` 無法建立。因此本次無法重驗報告的「40 項全部通過」。  
   證據：[test_clients.py:16](../tests/test_clients.py#L16)、[test_clients.py:29](../tests/test_clients.py#L29)、[test_clients.py:46](../tests/test_clients.py#L46)、[conftest.py:7](../tests/conftest.py#L7)、[step3-report.md:12](../docs/step3-report.md#L12)。