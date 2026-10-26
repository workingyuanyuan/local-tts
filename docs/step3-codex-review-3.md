**需先修正。** 第二輪第 1 項的牆鐘上限修正引入背景請求問題，會造成遺失工作 ID，以及 CLI 無法及時退出。

1. **阻擋：逾時後的 HTTP 請求仍會繼續執行，排隊中的生成請求甚至會延後送出。**  
   `_request` 只停止等待 Future，沒有取消排隊工作或中止正在執行的請求。記憶體重現：8 個慢請求占滿執行緒後，新的 `submit` 先回 `server_timeout`；釋放執行緒後，該 POST 才送出，回傳的工作 ID 已無人接收，可能導致呼叫端再次生成。此外，CLI 重現中 `main` 在 **0.302 秒**回傳錯誤，程序卻到 **2.101 秒**才退出；持續回傳資料的請求可持續拖住退出。需處理逾時請求的取消、資源釋放與程序結束，並補上這兩種回歸測試。  
   證據：[client.py:42](../src/local_tts/client.py#L42)、[client.py:174](../src/local_tts/client.py#L174)、[client.py:177](../src/local_tts/client.py#L177)、[mcp_server.py:114](../src/local_tts/mcp_server.py#L114)。

2. **建議：驗收腳本仍有路徑引用問題。**  
   Windows 清理使用 `xargs rm -f`，路徑含空白時會拆成多個刪除目標；stdin 測試將輸出路徑嵌入 `bash -c` 字串，目錄含單引號時會產生語法錯誤。應完整保留路徑邊界。  
   證據：[step3_run.sh:44](../scripts/step3_run.sh#L44)、[step3_run.sh:37](../scripts/step3_run.sh#L37)。

3. **建議：報告宣稱共同選項「可放在任何位置」，與 CLI 不符。**  
   實測 `tts --json health` 回 exit 2；`--json`、`-q` 僅註冊於子命令。應修正敘述或補足解析能力。  
   證據：[step3-report.md:8](../docs/step3-report.md#L8)、[cli.py:133](../src/local_tts/cli.py#L133)、[cli.py:137](../src/local_tts/cli.py#L137)。

4. **備註：`agent_claude.json` 與本次產物不符。**  
   該檔記錄 **2.96 秒**、job `3c20cef5b2e2412e`；本次 `agent_claude.jsonl` 記錄 **3.04 秒**、job `b60e06388bf84256`，與音檔相符。應明確區分這兩份證據，避免將 JSON 當成本次結果。  
   證據：[agent_claude.json:1](../outputs/step3/final/agent_claude.json#L1)、[agent_claude.jsonl:6](../outputs/step3/final/agent_claude.jsonl#L6)、[summary.txt:26](../outputs/step3/final/summary.txt#L26)。

5. **備註：第二輪第 2～6 項修正核對通過。**  
   第 2 項依工作格式解析無副檔名路徑；第 3 項固定每工作一把鎖並快取各路徑結果；第 4 項接受覆寫重試；第 5 項補齊送出後與恢復失敗的工作資訊；第 6 項嚴格解碼 stdin。另已核對快照 `format` 與下載前格式檢查一致。關鍵 MCP 情境另以不寫檔的假 client 重現通過。  
   證據：[mcp_server.py:61](../src/local_tts/mcp_server.py#L61)、[mcp_server.py:82](../src/local_tts/mcp_server.py#L82)、[mcp_server.py:125](../src/local_tts/mcp_server.py#L125)、[cli.py:87](../src/local_tts/cli.py#L87)、[cli.py:54](../src/local_tts/cli.py#L54)、[scheduler.py:100](../src/local_tts/server/scheduler.py#L100)、[client.py:238](../src/local_tts/client.py#L238)。

6. **備註：第二輪第 7 項的主要驗收補強已有證據。**  
   腳本已檢查 Agent 退出碼、工具事件、預期檔案、精確聲道與權限。Claude 有對應的 tool_use／completed tool_result，Codex 有完成事件。報告的長文 **20.0／45.0／38.1 秒、108.68 秒音訊**與原始輸出一致；8 個音檔均完整解碼成功，為 **48000 Hz、2 聲道、0644**。  
   證據：[step3_run.sh:58](../scripts/step3_run.sh#L58)、[step3_run.sh:94](../scripts/step3_run.sh#L94)、[agent_claude.jsonl:5](../outputs/step3/final/agent_claude.jsonl#L5)、[agent_codex.log:16](../outputs/step3/final/agent_codex.log#L16)、[mcp_stdio.json:30](../outputs/step3/final/mcp_stdio.json#L30)、[mcp_stdio.json:60](../outputs/step3/final/mcp_stdio.json#L60)、[summary.txt:20](../outputs/step3/final/summary.txt#L20)。

7. **備註：本次無法完整重驗「47 項通過」。**  
   指定命令在 capture 初始化時因無可用暫存目錄中止。改用記憶體 capture，並以定時喚醒處理沙箱的事件迴圈限制後，結果為 **16 passed、30 setup errors、1 failed**。30 項未進入測試本體：`test_clients.py` 除 `test_mcp_tools_listed`、`test_server_unreachable_is_reported`、`test_request_wall_clock_limit_with_trickling_response` 外的 **22 項**，以及 `test_server.py` 全部 **8 項**，均受 `tmp_path` 限制；另 1 項慢速回應測試在建立 socket 時遭沙箱拒絕。  
   證據：[test_clients.py:16](../tests/test_clients.py#L16)、[test_clients.py:250](../tests/test_clients.py#L250)、[conftest.py:7](../tests/conftest.py#L7)、[step3-report.md:12](../docs/step3-report.md#L12)。