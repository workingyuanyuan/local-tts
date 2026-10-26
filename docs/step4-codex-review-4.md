第三輪十項 finding／suggestion 核對如下：

| 項目 | 狀態 | 核對結果 |
|---|---|---|
| #1 `minor` 自動放行 | 已修正 | 已移除；原有多唸 `not`／「不」、冠詞缺漏案例均不再直接通過。 |
| #2 unequal replace 補足 `heard` | 已修正 | [asr_compare.py:155](../scripts/asr_compare.py#L155) 僅接受相符或同長替換；`do not → now` 回歸案例符合預期。 |
| #3 剪段新增異常 | 部分修正 | 可靠錨點之間會拒絕新異常；錨點外仍可能放行真實短插入，見下方。 |
| #4 空裁切窗口 | 已修正 | [asr_compare.py:129](../scripts/asr_compare.py#L129) 將超界候選裁成有效檔尾窗口；`(12,14,10)` 得 `[8,10]`。 |
| #5 pending 覆寫設定 | 已修正 | [mcp_server.py:109](../src/local_tts/mcp_server.py#L109) 同路徑保留授權，`next.arguments` 帶 `overwrite`；測試涵蓋 MCP 重啟。 |
| #6 CLI 終止錯誤接續 | 已修正 | [client.py:68](../src/local_tts/client.py#L68) 共用 `resumable()`；CLI 兩分支均排除 `unknown_job`／failed。 |
| #7 引號路徑與恢復指引 | 已修正 | [mcp_server.py:79](../src/local_tts/mcp_server.py#L79) 使用 JSON recovery、結構化 `next`，工具說明要求原路徑。 |
| #8 英數估時與獨立樣本 | 部分修正 | 混合英數、縮寫已處理；固定時長測試仍依賴估時，另有全大寫誤判。 |
| #9 CLI 子命令偵測 | 已修正 | [step4_evidence.py:26](../scripts/step4_evidence.py#L26) 涵蓋全部五個合法子命令及共用選項。 |
| #10 原始 ASR 證據 | 已修正 | [asr_compare.py:238](../scripts/asr_compare.py#L238) 保存原始字詞時間戳、剪段時間戳、兩種雜湊、revision、設定及 `span_peak_dbfs`。 |

`message` 為字串的修正也成立：[step4_evidence.py:88](../scripts/step4_evidence.py#L88) 已正確處理；六份 final transcript 重解析均與保存的 evidence 一致。

本輪發現：

1. **阻擋｜[scripts/asr_compare.py:158](../scripts/asr_compare.py#L158)、[同檔:235](../scripts/asr_compare.py#L235)：邊緣容許仍會放行真實短插入。**

   已用注入轉寫重現：

   ```text
   原文：Start this backup process now keep one backup off site.
   首遍：Start this backup process now do not keep one backup off site.
   剪段：now do not keep one backup off site
   cut_start=True → resolved=True
   ```

   剪段在 `do not` 前已聽到完整的 `now`；但它不足兩詞，不算可靠錨點，`do not` 因而被當成容許的兩詞亂字。中文多唸「不要」也能重現。

   **修正：**候選插入在剪段再次出現時，不得套用邊緣容許；使用剪段時間戳確認異常是否真正接觸裁切點，否則擴窗或保留 unresolved。加入上述中英文案例。

2. **阻擋｜[scripts/step4_check.py:80](../scripts/step4_check.py#L80)：整段 SNR 掩蓋局部漏字，整片 ASR 改為 INFO 的前提不足。**

   以 final Codex 影片的 PCM，將 **15.10–15.36 秒**靜音，刪掉第三段的 `one`；`soundtrack()` 仍無問題，該段 SNR **11.5 dB**，定位與場景切點均維持通過。

   **修正：**定位後逐一檢查有效語音短窗的局部殘差或有聲覆蓋，容差用正常 AAC 樣本校準；加入單字靜音與局部新增語音案例。完成前，成片 ASR 異常仍須確認。

3. **應修正｜[scripts/step4_check.py:87](../scripts/step4_check.py#L87)：段外不足一窗的聲音完全漏檢。**

   計算捨棄最後不足 400 samples 的部分。在 final Codex 音軌後追加 **25 毫秒、振幅 0.8** 的聲音，仍通過，甚至回報段外 `−120 dBFS`。

   **修正：**逐一檢查未覆蓋的連續區間，包含不足一窗的末端；明確區分目前的短窗 RMS 與報告所稱最大音量。

4. **應修正｜[src/local_tts/text.py:41](../src/local_tts/text.py#L41)、[tests/test_engine.py:196](../tests/test_engine.py#L196)：普通全大寫英文會被當成逐字母拼讀，測試未攔住此回歸。**

   固定輸出 3 秒時：

   - `the quick brown fox jumps over the lazy dog.`：估時 3.06 秒，通過。
   - 同句全大寫：估時 11.9 秒，過短門檻 3.07 秒，重試後失敗。

   現有「正常慢速」與邊界音訊仍由 `estimate_seconds()` 算出，因此 [報告:65](../docs/step4-report.md#L65) 的「不依賴估計函式」不成立。

   **修正：**區分普通大寫詞與縮寫，或分開生成預算的保守上限與過短判定下限；加入直接寫定時長的正常樣本及大小寫等價測試，修正報告。

5. **應修正｜[scripts/step4_check.py:228](../scripts/step4_check.py#L228)、[同檔:234](../scripts/step4_check.py#L234)：聆聽覆核未綁定目前的檔案內容。**

   `asr()` 不核對現存音訊／原文與保存雜湊。以第八輪結果改成錯誤的音訊雜湊或不存在的原文路徑，仍可 PASS；空白的「誰聽過與聽到什麼」也被接受。報告中的單獨覆核命令因此可能採信舊結果。

   **修正：**核對 pair 兩個檔案的當前雜湊，並要求非空的聆聽者與結論；結果過期時要求重跑 ASR。

6. **應修正｜[scripts/step4_check.py:100](../scripts/step4_check.py#L100)：場景閾值會拒絕符合 prompt 的正常影片。**

   已生成三張白底、中央分別顯示 `First／Second／Third`、各七秒的標題卡；偵測切點為 **零個**。短標題改變不足以跨過全畫面的 `scene > 0.01`。

   **修正：**依場景清單驗證各區間的代表畫面與內容變化，加入同底色、不同短標題案例；偵測不確定時留下明確覆核結果。

7. **應修正｜[scripts/step4_check.py:120](../scripts/step4_check.py#L120)：反例重檢會算入任務結束後的新音檔。**

   `new_audio()` 只有 `mtime >= start`，沒有結束界線。正確反例完成後，其他任務在預設輸出目錄生成音訊，之後 `STEP4_RECHECK=1` 就可能誤判反例失敗。

   **修正：**原始執行時保存目錄前後差集與服務日誌窗口，重檢使用保存的證據；一併保留原始 journal 內容，避免只有零工作計數。

8. **建議｜[scripts/step4_check.py:188](../scripts/step4_check.py#L188)、[scripts/asr_compare.py:155](../scripts/asr_compare.py#L155)：另有兩種誤報。**

   檔名字典序未必是場景順序，例如 `intro → method → conclusion` 會被重新排序；應使用明確時間軸。剪段驗證也未沿用主流程的空格等價規則，完整辨識的 `back up` 可能因原文為 `backup` 而失敗；應共用等價判定。

第八輪報告對結果的敘述正確：Claude 影片流程 PASS、`scene03.wav` 保持 ASR FAIL；Codex 正例及重跑反例 PASS。`recheck.txt` 也交代了首跑退出碼及正例手動補建 `.run` 的來源。疑似缺陷仍待使用者聆聽，沒有被寫成已確認正常。

保存證據方面，八份音檔及兩部影片均可完整解碼，副本雜湊吻合；十五項 ASR 驗證、十三項 ASR 自測與三項音軌自測符合保存結果。上述漏判案例尚未被這些測試涵蓋，仍影響 `CLAUDE.md`「無漏句、無截斷」的驗收依據。

需先修正