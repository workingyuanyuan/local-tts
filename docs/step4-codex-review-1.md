1. **應修正｜[scripts/install_agent_rules.sh:23](../scripts/install_agent_rules.sh#L23)**  
   未閉合的 `local-tts:begin` 會造成資料遺失：第一次安裝追加完整區塊，第二次會從舊 BEGIN 替換到新 END，刪掉中間的使用者規則。已在記憶體重現，也違反報告與 README 的冪等、保留內容聲稱。**修正：**寫入前驗證標記為零組或唯一完整的一組；缺漏、重複、巢狀時停止。完整產生新內容後再原子替換原檔。

2. **應修正｜[scripts/step4_run.sh:30](../scripts/step4_run.sh#L30)**  
   `setup` 無條件 `rm -rf` 任務目錄；任務參數也未驗證，帶 `../` 可讓刪除越出 WORK。正常重跑亦會刪除同名目錄內的既有使用者檔案。**修正：**先以白名單驗證所有任務名稱，使用新建的專用暫存工作目錄；清理前驗證實際路徑與本次執行的所有權標記。

3. **應修正｜[Claude SKILL.md:50](../agent-rules/claude/narration/SKILL.md#L50)、[Codex 規則:10](../agent-rules/codex/AGENTS.local-tts.md#L10)**  
   「任何帶 `job_id` 的錯誤都用 `get_speech_job` 恢復」也涵蓋終止失敗。`client.py:147` 會對 failed job 重拋相同錯誤，因此 `segment_failed`、`segment_truncated` 可能進入無效輪詢，並與後面的重新生成指示衝突。**修正：**區分可恢復的傳輸／下載／寫檔錯誤與終止合成失敗；後者修正原因後建立新 job。同步修正 MCP 的通用 `recovery` 訊息。

4. **應修正｜[Claude SKILL.md:50](../agent-rules/claude/narration/SKILL.md#L50)、[Codex 規則:10](../agent-rules/codex/AGENTS.local-tts.md#L10)**  
   恢復只教傳 `job_id`。MCP 程序重啟後會遺失原始路徑，`mcp_server.py:90` 起會改寫到預設輸出目錄，違反產物必須留在任務專案的規則。**修正：**要求保存原始絕對路徑，恢復時明確傳入 `get_speech_job(job_id, output_path=原始路徑)`。

5. **應修正｜[Claude SKILL.md:47](../agent-rules/claude/narration/SKILL.md#L47)、[Codex 規則:9](../agent-rules/codex/AGENTS.local-tts.md#L9)**  
   要求文章、故事、有聲內容全文一次送出，卻未說明 API 上限：20,000 字元、單一 pause 最多 60 秒、pause 合計最多 600 秒；超限會被拒絕。**修正：**列出限制及 `input_too_long`／`invalid_pause` 處理；超長全文依段落分批，固定 voice，依序生成後合併。

6. **應修正｜[Claude SKILL.md:21](../agent-rules/claude/narration/SKILL.md#L21)、[Codex 規則:5](../agent-rules/codex/AGENTS.local-tts.md#L5)**  
   CLI 範例沒有 `--timeout`，預設無限等待；`--json` 又抑制進度，因此外層命令逾時中斷時，Agent 可能拿不到已成立工作的 `job_id`，繼而重送。**修正：**加入有限等待，例如 `--timeout 45`；明確說明 exit 3 代表 pending，應接續返回的 `fetch_command`，後續等待也設定期限。

7. **應修正｜[scripts/step4_run.sh:105](../scripts/step4_run.sh#L105)、[同檔:147](../scripts/step4_run.sh#L147)**  
   驗收解析失敗可能仍 PASS：evidence 解析失敗被替換成 `{}`，反例會把它視為零次呼叫；ASR JSON 解析在 process substitution 中失敗，也不會設定 `FAIL`。後者已重現為解析程序失敗但 `FAIL=0`。**修正：**明確檢查每個產生／解析程序的退出碼與資料結構，解析失敗一律 FAIL，並核對預期結果筆數。

8. **應修正｜[scripts/step4_evidence.py:53](../scripts/step4_evidence.py#L53)、[同檔:81](../scripts/step4_evidence.py#L81)**  
   `generate_speech` 若先回帶 `job_id` 的錯誤，再成功恢復，解析器不會把原文綁到該 job。已重現 `speech_completed=1`、`scripts={}`；驗收因此可能完全跳過這個成功音檔的 ASR，仍 ALL PASS。**修正：**從錯誤結果擷取 job ID 並保留原文關聯；要求每個最終完成的唯一音檔都有對應原文與 ASR 結果。

9. **應修正｜[scripts/step4_run.sh:119](../scripts/step4_run.sh#L119)**  
   「至少一個 completed」與「工作目錄有音檔」分開判斷，沒有確認兩者是同一檔案。MCP 寫到專案外，再於工作目錄建立另一個可解碼音檔，也可能通過。**修正：**逐一驗證 completed 回傳路徑解析後位於任務目錄內，並以這些確切檔案進行存在性、解碼與 ASR 檢查；產物副本應核對雜湊。

10. **應修正｜[scripts/step4_run.sh:130](../scripts/step4_run.sh#L130)、[同檔:160](../scripts/step4_run.sh#L160)**  
    影片驗收容許 15–40 秒，未落實 prompt 的 20–30 秒，也未驗證三個場景及逐段對齊。成品 ASR 只列印文字；靜音、錯誤旁白或漏段，只要 ASR 程序成功仍可能 PASS。[報告:53](../docs/step4-report.md#L53) 的「確認合成後音軌就是旁白」因此不是自動斷言所證明。**修正：**比對成品 ASR 與最終旁白稿，斷言 20–30 秒及場景時間；人工確認項目另記錄明確結論。

11. **應修正｜[scripts/step4_run.sh:115](../scripts/step4_run.sh#L115)、[scripts/step4_evidence.py:76](../scripts/step4_evidence.py#L76)**  
    反例的「沒有 TTS 呼叫」只計算指定格式的 MCP 事件；Codex 更只計 completed 事件。規則允許的 CLI fallback，以及寫到預設目錄的音檔，都可能避開檢查。**修正：**計算開始、失敗與完成的生成嘗試，檢查 shell／CLI 呼叫；更可靠的做法是以服務端請求紀錄核對反例期間沒有生成請求。

12. **應修正｜[scripts/step4_run.sh:152](../scripts/step4_run.sh#L152)**  
    ASR 只看 `missing_sentences`，忽略 `deletions` 與 error rate。句子保留六成即可 PASS，因此尾句截掉一大段仍可能通過；例如刪掉備份規則中的「off site」仍有八成匹配。**修正：**增加刪除片段與尾句完整性檢查；疑似 ASR 誤差用局部重轉寫或人工聆聽確認，保存確認證據後再判定。

13. **建議｜[Claude SKILL.md:70](../agent-rules/claude/narration/SKILL.md#L70)**  
    `gpu_out_of_memory` 被直接歸因於其他程式占用 GPU，但程式只確認發生 OOM，沒有確認原因。**修正：**改成「顯存不足」，先查 `tts_status`、GPU 使用情況與日誌，再向使用者說明原因。

14. **建議｜[Claude SKILL.md:34](../agent-rules/claude/narration/SKILL.md#L34)**  
    「單次呼叫內音色一致」說得過於確定。`engine.py:179` 只有取得至少三秒的有效參考音訊後才沿用；之前的短段可能分別生成音色。**修正：**描述為盡力維持；一致性必要時明確指定固定 voice，超長分批時尤其如此。

15. **建議｜[docs/step4-report.md:31](../docs/step4-report.md#L31)、[同檔:56](../docs/step4-report.md#L56)**  
    報告稱腳本平行執行兩個 Agent，但單次腳本的任務迴圈是循序執行；應記錄實際使用的兩個平行命令與退出碼。另外，數字正規化不能把「Point one」與「Point 2」視為相同；第一輪保存的原稿對應的是「Point two」。**修正：**補上實際執行命令，並更正數字範例。

需先修正