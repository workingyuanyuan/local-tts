**需先修正。** 第 4 輪三項修正已有支持證據，但 async 保存流程引入一項可重現的取消問題。

1. **阻擋：取消保存後，背景寫檔仍執行，重試會破壞冪等性。**  
   `save` 使用 `asyncio.to_thread(write_file, …)`；呼叫遭取消時，執行緒繼續寫檔，`_deliver` 卻立即釋鎖，且尚未更新保存快取。無寫檔實驗重現：取消後為「鎖已釋放、1 個 writer、快取空白」；立即重試同一路徑後出現 **2 個並行 writer**，重試回覆 `output_exists`。應讓寫檔、快取更新與鎖具有一致生命週期，並補上取消後重試測試。  
   證據：[client.py:257](../src/local_tts/client.py#L257)、[mcp_server.py:104](../src/local_tts/mcp_server.py#L104)、[mcp_server.py:109](../src/local_tts/mcp_server.py#L109)。

2. **建議：修正期限保證及報告敘述。**  
   `asyncio.timeout()` 屬合作式取消，無法中斷同步 gzip 解碼，因此「解碼任一階段超時都會取消」過強。記憶體實驗以壓縮的 32 MiB 資料驗證，期限 `.005` 秒仍於約 `.075` 秒成功返回。另外，第 2 輪對照表仍稱最終方案為串流逐區塊檢查，應統一為目前的 async 實作。  
   證據：[client.py:194](../src/local_tts/client.py#L194)、[step3-report.md:17](../docs/step3-report.md#L17)、[step3-report.md:76](../docs/step3-report.md#L76)。

3. **備註：第 4 輪三項修正逐項核實成立。**  
   慢速標頭、本體及 gzip 等待，保留真實 HTTPTransport／httpcore、替換記憶體連線後，`.05` 秒期限均於約 `.051–.052` 秒取消並關閉連線；合法 gzip 回應正常解碼一次。實測腳本也已清除舊 JSON，並於 `finally` 保存本輪失敗資訊。  
   證據：[client.py:176](../src/local_tts/client.py#L176)、[client.py:194](../src/local_tts/client.py#L194)、[step3_run.sh:26](../scripts/step3_run.sh#L26)、[step3_mcp_stdio.py:19](../scripts/step3_mcp_stdio.py#L19)。

4. **備註：既有恢復與保存修正有實作及記憶體驗證支持。**  
   無副檔名重複取回、換路徑並行保存、覆寫重試、錯誤附工作資訊及重啟後格式檢查均通過；快照提供 `format`，CLI 已使用 async client。  
   證據：[mcp_server.py:80](../src/local_tts/mcp_server.py#L80)、[mcp_server.py:127](../src/local_tts/mcp_server.py#L127)、[scheduler.py:100](../src/local_tts/server/scheduler.py#L100)、[cli.py:178](../src/local_tts/cli.py#L178)。

5. **備註：本輪產物與報告數字一致。**  
   長文為 **20.0／45.0／38.2 秒、音訊 108.63 秒**；Claude、Codex 均有工具完成事件。8 個音檔完整解碼成功，皆為 **48000 Hz、2 聲道、0644**。報告使用本輪 `agent_claude.jsonl`；README 主要流程敘述與程式一致，期限保證受第 2 項限制。  
   證據：[mcp_stdio.json:30](../outputs/step3/final/mcp_stdio.json#L30)、[summary.txt:20](../outputs/step3/final/summary.txt#L20)、[agent_claude.jsonl:7](../outputs/step3/final/agent_claude.jsonl#L7)、[agent_codex.log:19](../outputs/step3/final/agent_codex.log#L19)。

6. **備註：本次無法完整重驗報告的 50 項通過。**  
   指定命令在 capture 初始化時因無可用暫存目錄中止。改用記憶體 capture 後為 **16 passed、30 setup errors、4 failed**。未進入測試本體的是 `test_clients.py` 依賴 `tmp_path` 的 **22 項**及 `test_server.py` 全部 **8 項**；另有三種階段的期限測試及 gzip 解碼測試，在建立 socket 時遭沙箱拒絕。  
   證據：[test_clients.py:16](../tests/test_clients.py#L16)、[test_clients.py:278](../tests/test_clients.py#L278)、[test_clients.py:303](../tests/test_clients.py#L303)、[conftest.py:7](../tests/conftest.py#L7)。