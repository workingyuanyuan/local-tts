**結論：需先修正。** 截斷重試、模型載入失敗清理及工作音訊端點有可重現缺陷，會影響第 3 步 MCP／CLI 的正確性。

1. **【阻擋】截斷重切會觸發 Tensor 布林判斷例外。**  
   [engine.py:140](../src/local_tts/engine.py#L140) 使用 `ref_out = ref_out or ref`，但真實 adapter 回傳的是多元素 Tensor（[moss_local.py:110](../src/local_tts/adapters/moss_local.py#L110)）。第一個子段取得參考後，第二個子段便可能拋出 `Boolean value of Tensor with more than one value is ambiguous`，讓原本應恢復的截斷工作失敗。已用 CPU Tensor 重現；[test_engine.py:38](../tests/test_engine.py#L38) 使用字串參考，掩蓋此問題。應改為明確檢查 `None`，並補 Tensor 參考的測試。

2. **【阻擋】模型載入失敗後，錯誤紀錄會保留載入中的 GPU 資源。**  
   [moss_local.py:60](../src/local_tts/adapters/moss_local.py#L60) 先把 codec 搬到 GPU，到第 65 行才將 processor／model 存入實例；中途失敗時，`unload()` 清不到區域變數。[engine.py:56](../src/local_tts/engine.py#L56) 保留例外鏈，[scheduler.py:145](../src/local_tts/server/scheduler.py#L145) 又將例外存進工作紀錄，traceback 因而持續持有資源。  
   已用實際 Scheduler／ModelManager 與假載入失敗重現：manager 顯示未載入，資源經 GC 仍存活，清除 `job.error` 才釋放。實際模型搬移時若 OOM，後續請求可能持續 OOM。應確保部分載入資源能清理，工作紀錄只保存序列化錯誤資料。

3. **【阻擋】過長中文引文可能完全無法切細。**  
   [text.py:104](../src/local_tts/text.py#L104) 的 `\S+\s*` 會從開頭引號一次吞掉後續無空白中文。已重現：`「`＋2,000 個中文字＋`」`，以 30 秒設定切段，仍得到單一估計 **400 秒**的段落，`split_finer()` 也只回傳一段。若生成達 4,096 frames 上限，就會直接 `segment_truncated`。應讓中文在引號或其他非中文字之後仍可逐字切分，並驗證重切確實縮短段落。

4. **【阻擋】截斷重切未保留停頓與音色一致性契約。**  
   [engine.py:137](../src/local_tts/engine.py#L137) 直接遞迴生成子段，沒有重新處理邊界停頓；隨後 [engine.py:142](../src/local_tts/engine.py#L142) 的 `trim_silence()` 會刪除移到子段開頭／結尾的 `[pause]`。例如 40 字＋`[pause 5s]`＋40 字，重切後可遺失要求的 5 秒靜音。  
   同處也一直傳入原始 `reference`：無 voice 的首段重切時，各子段全部收到 `None`，直到整段完成後才更新參考（[engine.py:165](../src/local_tts/engine.py#L165)），各子段因此可能生成不同音色。應讓重切子段共用正常切段的停頓與參考處理流程。

5. **【阻擋】下載未完成工作的音訊會回 500。**  
   [app.py:173](../src/local_tts/server/app.py#L173) 呼叫 `TTSError(..., 409, status=job.status)`，重複指定 [errors.py:7](../src/local_tts/errors.py#L7) 的 `status` 參數。已重現 `TypeError`；queued／running 工作無法取得契約中的 `409 job_not_finished`。應將工作狀態明細改用其他名稱。

6. **【建議】閒置卸載與 submit 存在競態，工作狀態也缺少一致快照。**  
   [scheduler.py:113](../src/local_tts/server/scheduler.py#L113) 檢查空佇列後，到卸載前可插入新請求；已以執行緒同步重現「佇列已有一筆工作仍卸載」。這違反 [CLAUDE.md:43](../CLAUDE.md#L43)，會增加一次冷載入延遲。  
   此外，[scheduler.py:33](../src/local_tts/server/scheduler.py#L33) 與 [app.py:110](../src/local_tts/server/app.py#L110) 分開讀取狀態與佇列位置，可能回傳 `queued` 搭配 `queue_position=null`，或 `succeeded` 搭配 `finished=null`。應將卸載決策與提交協調，並提供一致的工作快照。

7. **【建議】TTL 清理依賴後續請求，同步等待沒有期限。**  
   [scheduler.py:95](../src/local_tts/server/scheduler.py#L95) 的清理只由 submit／get 觸發；服務閒置後，過期音訊仍可能留在記憶體。[app.py:154](../src/local_tts/server/app.py#L154) 則無限等待完成事件。若生成或編碼卡住，單一 worker、後續工作與同步呼叫都會持續等待；完成後 TTL 無法處理此情況。建議週期性清理，並明定同步等待期限及執行中工作的逾時處理。

8. **【建議】報告的效能與排程結論缺少可追溯原始紀錄。**  
   [step2_long.py:30](../scripts/step2_long.py#L30) 只將結果印到 stdout；所附產物未保存工作結果、GPU 採樣及服務日誌，因此 [step2-report.md:29](../docs/step2-report.md#L29)、[step2-report.md:35](../docs/step2-report.md#L35)、[step2-report.md:61](../docs/step2-report.md#L61) 的耗時、峰值顯存、零重試與卸載時間無法獨立核對。  
   [step2_concurrency.py:44](../scripts/step2_concurrency.py#L44) 僅印出開始順序，未比對服務端入列順序，也未斷言接受數與成功數；即使改成 LIFO 或所有工作失敗，腳本仍可正常結束。修正後應保存完整結果並加入斷言。

9. **【建議】ASR 與說話人評估的結論需要收斂，評估環境需要固定。**  
   [asr_compare.py:91](../scripts/asr_compare.py#L91) 忽略少於四個中文字或兩個英文詞的刪除，因此完整漏掉 `Stop.` 仍可能得到 `deletions=[]`。  
   [speaker_consistency.py:24](../scripts/speaker_consistency.py#L24) 使用固定六秒視窗、略過尾端不足六秒部分，只與開頭比較；結果支持串接改善一致性，但不足以證明報告所稱「每段都是不同音色」。其套件版本仍有 `<ver>`，評估模型 revision 也未固定（[speaker_consistency.py:4](../scripts/speaker_consistency.py#L4)、[asr_compare.py:80](../scripts/asr_compare.py#L80)）。應補逐句／逐段核對及完整評估版本。

10. **【備註】已核對的正常路徑與產物數字吻合。**  
    9 個測試全部通過，但未涵蓋上述真實參考型別、API 與排程失敗路徑。模型／voice 預設值、未知項目錯誤、清單欄位及不支援參數處理符合目前契約（[config.py:59](../src/local_tts/config.py#L59)、[app.py:77](../src/local_tts/server/app.py#L77)）。  
    截斷 frame 計算符合 remote code：生成迴圈抽到結束 token 時直接停止，未附加結束列；[moss_local.py:101](../src/local_tts/adapters/moss_local.py#L101) 沒有 off-by-one。四個長文時長為 **144.390／165.510／114.920／105.880 秒**；保存逐字稿重新計算的 CER／WER、說話人相似度均與報告吻合（[asr_zh.jsonl:1](../outputs/step2/asr_zh.jsonl#L1)、[asr_en.jsonl:1](../outputs/step2/asr_en.jsonl#L1)、[speaker_consistency.jsonl:1](../outputs/step2/speaker_consistency.jsonl#L1)）。13 個音檔皆可完整解碼，均為 48 kHz 立體聲。