**需先修正。** 第 3 輪第 1 項的整體期限仍有漏洞；第 2～4 項修正已核實。

1. **阻擋：串流截止未涵蓋完整 HTTP 請求。**  
   截止時間只在 `iter_bytes()` 產生已解碼資料後檢查；接收完整回應標頭之前，以及 gzip 解碼器尚未輸出資料時，都不會執行檢查。保留真實 HTTPTransport／httpcore、僅替換記憶體 socket，每 0.1 秒回傳一個 byte，`timeout=0.5` 的慢速標頭與 gzip 情境均約 **3.21 秒**才截止，超過報告宣稱的「最多超出約 2 秒」。延長上述輸入仍可繼續拖延，初次生成呼叫因此仍可能未及交付 `job_id` 就遭 MCP 逾時。需涵蓋這些階段並補回歸測試。  
   證據：[client.py:178](../src/local_tts/client.py#L178)、[test_clients.py:247](../tests/test_clients.py#L247)。因此 [step3-report.md:17](../docs/step3-report.md#L17) 與 [README.md:77](../README.md#L77) 的時間上限尚無保證。

2. **建議：合法 gzip 回應會被二次解碼。**  
   `iter_bytes()` 已完成解壓縮，重建 `Response` 卻保留 `Content-Encoding: gzip`，導致再次解碼。記憶體驗證得到 `DecodingError: incorrect header check`。目前直連服務未啟用 gzip；使用壓縮代理或自訂服務 URL 時會觸發。  
   證據：[client.py:180](../src/local_tts/client.py#L180)、[client.py:184](../src/local_tts/client.py#L184)。

3. **建議：MCP 實測失敗時可能留下上輪 JSON。**  
   重跑開始時未清除 `mcp_stdio.json`，而該檔只在全部成功後寫入。中途失敗會遺失本輪紀錄並保留舊結果；應清除舊檔並保存當輪失敗資訊。  
   證據：[step3_run.sh:26](../scripts/step3_run.sh#L26)、[step3_mcp_stdio.py:38](../scripts/step3_mcp_stdio.py#L38)。

4. **備註：其餘修正核對通過。**  
   固定工作鎖、各路徑冪等保存、覆寫重試、錯誤恢復資訊與格式恢復均有實作及記憶體驗證支持；CLI 共同選項、stdin 路徑引用、Windows 清理及舊 Agent 證據清除亦已修正。  
   證據：[mcp_server.py:82](../src/local_tts/mcp_server.py#L82)、[scheduler.py:100](../src/local_tts/server/scheduler.py#L100)、[cli.py:134](../src/local_tts/cli.py#L134)、[step3_run.sh:26](../scripts/step3_run.sh#L26)。

5. **備註：本次實測產物與報告數字一致。**  
   長文為 **20.0／45.0／44.1 秒、112.85 秒音訊**；Claude／Codex 均有工具完成事件。8 個音檔全部完整解碼，皆為 **48000 Hz、2 聲道、0644**。  
   證據：[mcp_stdio.json:30](../outputs/step3/final/mcp_stdio.json#L30)、[summary.txt:20](../outputs/step3/final/summary.txt#L20)、[agent_claude.jsonl:5](../outputs/step3/final/agent_claude.jsonl#L5)、[agent_codex.log:18](../outputs/step3/final/agent_codex.log#L18)。

6. **備註：唯讀沙箱無法完整重驗 47 項測試。**  
   指定命令在輸出擷取初始化時因無可用暫存目錄中止。改用記憶體擷取並定時喚醒事件迴圈後，結果為 **16 passed、30 setup errors、1 failed**。30 項未進入測試本體：`test_clients.py` 除 `test_mcp_tools_listed`、`test_server_unreachable_is_reported`、`test_request_wall_clock_limit_with_trickling_response` 外的 22 項，以及 `test_server.py` 全部 8 項；均受 `tmp_path` 限制。慢速回應測試另因建立 socket 遭沙箱拒絕而失敗。  
   證據：[test_clients.py:16](../tests/test_clients.py#L16)、[test_clients.py:252](../tests/test_clients.py#L252)、[conftest.py:7](../tests/conftest.py#L7)。