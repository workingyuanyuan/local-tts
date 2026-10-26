**需先修正，暫不進入第 4 步。** 一般成功流程已有實測支持，但逾時恢復、取回冪等性及重啟後格式處理仍有可重現問題。

1. **阻擋：`wait_seconds` 不是整次 MCP 呼叫的期限。**  
   輪詢只在 HTTP 回應後檢查期限；送出工作、pending 時查詢 health、完成後下載音訊都另計時間，下載可再等 120 秒。預設等待 45 秒加上 health 的 30 秒，可能超過 60 秒；等待 300 秒後下載，也可能超過設定的 330 秒。初次呼叫因此逾時時，Agent 尚未取得 `job_id`，仍可能重送。記憶體驗證亦確認 `timeout=.001` 遇到耗時 `.04` 秒的查詢，仍等 `.04` 秒才返回。應建立整次呼叫共用的期限，並及時交付工作 ID。  
   證據：[client.py:159](../src/local_tts/client.py#L159)、[client.py:176](../src/local_tts/client.py#L176)、[client.py:184](../src/local_tts/client.py#L184)、[mcp_server.py:103](../src/local_tts/mcp_server.py#L103)。

2. **阻擋：工作成立後發生通訊錯誤，回覆遺失恢復資訊。**  
   `submit` 成功後，若第一次輪詢失敗，CLI／MCP 直接回傳 `server_unreachable`，沒有附上已知的 `job_id`、輸出路徑或工作仍可能執行中的說明。已用假 client 重現；呼叫端無法據此取回工作，容易再次生成。應在工作成立後的錯誤中保留恢復資訊。  
   證據：[cli.py:55](../src/local_tts/cli.py#L55)、[mcp_server.py:105](../src/local_tts/mcp_server.py#L105)、[client.py:131](../src/local_tts/client.py#L131)。

3. **阻擋：`get_speech_job` 的冪等性不完整。**  
   每次傳入 `output_path` 都會重建 `PendingJob`，清掉完成結果。完全相同的取回呼叫，第一次成功，第二次便重新寫檔而得到 `output_exists`；這是重啟恢復後自然會出現的重試方式。此外，同一工作的並行取回沒有鎖，兩次都可能進入 `save`，其中一次報錯。兩種情境均已用記憶體假 client 重現。應保留相同工作與路徑的完成結果，並序列化同一工作的保存操作。  
   證據：[mcp_server.py:130](../src/local_tts/mcp_server.py#L130)、[mcp_server.py:61](../src/local_tts/mcp_server.py#L61)。現有測試只涵蓋省略路徑的序列重試：[test_clients.py:120](../tests/test_clients.py#L120)。

4. **阻擋：MCP 重啟後恢復未完成工作，可能產生副檔名錯誤的音檔。**  
   queued／running 工作尚無 `result.format`，此時以 `fmt=None` 決定路徑；完成後不再核對實際格式。例如原工作是 FLAC，重啟後未指定路徑，便可能把 FLAC bytes 寫進預設 `.wav`；指定 `.wav` 也會被接受。已重現回傳 `path=/tmp/voice.wav, format=flac`。應取得實際格式後再決定或驗證輸出路徑。  
   證據：[mcp_server.py:132](../src/local_tts/mcp_server.py#L132)、[scheduler.py:105](../src/local_tts/server/scheduler.py#L105)、[client.py:177](../src/local_tts/client.py#L177)。

5. **阻擋：常見輸入與寫檔失敗無法提供承諾的結構化錯誤。**  
   MCP 只捕捉 `ClientError`；目錄權限不足、磁碟已滿等 `OSError` 會被 SDK 隱藏成單純的 `Error executing tool …`，沒有原因或錯誤碼。另有兩個已重現情境：輸出 `/` 觸發未捕捉的 `ValueError`；CLI 讀取非 UTF-8 檔案觸發未捕捉的 `UnicodeDecodeError`。應將這些預期失敗轉成穩定的結構化錯誤。  
   證據：[mcp_server.py:108](../src/local_tts/mcp_server.py#L108)、[client.py:70](../src/local_tts/client.py#L70)、[cli.py:45](../src/local_tts/cli.py#L45)、[cli.py:143](../src/local_tts/cli.py#L143)、[SDK base.py:211](../.venv/lib/python3.12/site-packages/mcp/server/mcpserver/tools/base.py#L211)。

6. **阻擋：環境變數可繞過絕對路徑契約。**  
   `LOCAL_TTS_OUTPUT_DIR=relative-dir` 時，未指定輸出路徑便直接回傳相對路徑，沒有經過絕對路徑檢查；實際輸出位置因 Agent 的工作目錄而異。此情境已重現。應對預設輸出目錄套用相同的路徑驗證。  
   證據：[client.py:23](../src/local_tts/client.py#L23)、[client.py:48](../src/local_tts/client.py#L48)；契約：[CLAUDE.md:66](../CLAUDE.md#L66)。

7. **建議：CLI 的 pending 取回指令需保留完整參數。**  
   提示直接插入未引用的路徑，也遺漏 `--url`、`--overwrite`。例如 `/tmp/a b.wav` 會產生無法照貼執行的指令；使用其他服務或覆寫既有檔案時，也可能無法取回。應使用 shell quoting 並保留相關選項。  
   證據：[cli.py:60](../src/local_tts/cli.py#L60)。

8. **建議：工具標註及權限說明應符合覆寫能力。**  
   寫入工具宣告 `destructive_hint=False`，但兩個工具都可透過 `overwrite=True` 覆蓋檔案。報告亦以「不覆寫既有檔案」說明免核准權限，可能讓後續使用規則建立在錯誤前提上。  
   證據：[mcp_server.py:34](../src/local_tts/mcp_server.py#L34)、[client.py:90](../src/local_tts/client.py#L90)、[step3-report.md:67](../docs/step3-report.md#L67)。

9. **建議：修正報告數字並補強驗證與證據保存。**  
   報告寫長文音訊 109 秒、最後等待 37 秒；final 原始資料為 **108.07 秒、38.1 秒**。腳本只列印取樣率、聲道及權限，即使規格錯誤仍可能 `ALL PASS`。此外，克隆語音產物隨後被覆寫；manual 沒有保存 Agent 呼叫紀錄，僅靠音檔無法獨立核對「由哪個 Agent 呼叫」及核准過程。應補上上述阻擋情境的回歸測試，並保存各次驗收產物與呼叫紀錄。  
   證據：[step3-report.md:39](../docs/step3-report.md#L39)、[mcp_stdio.json:60](../outputs/step3/final/mcp_stdio.json#L60)、[mcp_stdio.json:64](../outputs/step3/final/mcp_stdio.json#L64)、[step3_run.sh:53](../scripts/step3_run.sh#L53)、[step3_run.sh:26](../scripts/step3_run.sh#L26)、[step3-report.md:46](../docs/step3-report.md#L46)。

10. **備註：本次測試執行情況。**  
    指定命令在 pytest capture 階段因無可用暫存目錄而中止。改用記憶體 capture，並處理此沙箱的事件迴圈喚醒限制後，結果為 **16 passed、17 setup errors**。未進入測試本體的是 `test_clients.py` 中依賴 `tmp_path` 的 9 項，以及 `test_server.py` 全部 8 項；因此本次無法重驗報告的 33 項全部通過。另行直接執行 engine／manager 測試為 **14 passed**。  
    證據：[test_clients.py:14](../tests/test_clients.py#L14)、[conftest.py:7](../tests/conftest.py#L7)、[fakes.py:59](../tests/fakes.py#L59)。

11. **備註：原子寫入、實際產物與註冊已有支持證據。**  
    暫存檔建立於目標目錄，因此輸出到 `/mnt/c` 不需跨檔案系統 rename；不覆寫模式以 hard link 排他建立，覆寫使用 `os.replace`，一般例外由 `finally` 清理暫存檔。final 五個音檔本次均完整解碼成功，為 48 kHz、雙聲道、0644。Claude 與 Codex 的指定註冊均指向本專案 `tts-mcp`，Codex timeout 為 330；套件固定為 `mcp==2.3.0`。程式日誌及 SDK stdio 處理支持 stdout 通道正常。  
    證據：[client.py:82](../src/local_tts/client.py#L82)、[summary.txt:16](../outputs/step3/final/summary.txt#L16)、[config.toml:4](~/.codex/config.toml:4)、[pyproject.toml:17](../pyproject.toml#L17)、[uv.lock:563](../uv.lock#L563)、[mcp_server.py:173](../src/local_tts/mcp_server.py#L173)。