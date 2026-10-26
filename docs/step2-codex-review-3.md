**結論：可以進入第 3 步。** 第 2 輪的阻擋問題已修復；剩餘問題屬建議等級。

1. **【建議｜第 2 輪 #2】泰文硬切仍可能超過估計段長上限。**  
   `_hard_split()` 依整串平均密度計算固定字元塊長，沒有重新切分仍過長的子塊。以 2,280 字元的一般泰文測例呼叫 `split_text(text, 30)`，七段中有三段估計為 **36.04、35.70、36.04 秒**。文字完整，`split_finer()` 仍能繼續縮短，因此原本「完全無法切細」的阻擋已解除。建議逐塊重新估計並調整切點。證據：[text.py:116](../src/local_tts/text.py#L116)、[text.py:125](../src/local_tts/text.py#L125)。

2. **【建議｜第 2 輪 #6】解碼驗收仍未檢查 ffmpeg 退出碼。**  
   腳本以錯誤輸出是否空白判斷成功；已用回傳 **137、無錯誤輸出**的替身重現仍被記為 `decode=ok`。因此修正對照表所稱「解碼失敗即讓評估失敗」尚未完整成立。應同時檢查退出碼。卸載後 HTTP 200 與強制截斷次數斷言已正確補上。證據：[step2_eval.sh:25](../scripts/step2_eval.sh#L25)、[step2-report.md:104](../docs/step2-report.md#L104)、[step2_run.sh:57](../scripts/step2_run.sh#L57)、[step2_run.sh:81](../scripts/step2_run.sh#L81)。

3. **【建議】競態回歸測試沒有真正製造交錯。**  
   測試先等待工作變成 `failed`，才下載音訊；舊版競態實作也可能通過。建議以事件控制讀取與失敗完成的時序，並補入 janitor 在另一工作生成期間清理、最後工作音訊釋放的回歸測試。證據：[test_server.py:138](../tests/test_server.py#L138)、[test_server.py:144](../tests/test_server.py#L144)。

4. **【建議｜第 2 輪 #7】顯存數字正確，原因與服務歸屬應標為推估。**  
   **14,388 MiB** 的整卡取樣最大值正確，但出現在最後長文完成約 **27.7 秒後**。現有紀錄沒有程序級顯存或 allocated／reserved 時序，無法確定增加原因就是 caching allocator；扣起始基線所得「服務約 13.3 GiB」也依賴其他程序用量穩定的假設。證據：[step2-report.md:44](../docs/step2-report.md#L44)、[nvidia-smi.csv:764](../outputs/step2/final/nvidia-smi.csv#L764)、[server.log:623](../outputs/step2/final/server.log#L623)。

5. **【備註】其餘修正逐項核實通過。**

   | 第 2 輪項目 | 核實結果 |
   |---|---|
   | #1 outcome／下載競態 | 狀態、錯誤、音訊與 meta 在同一把鎖內取得；端點使用該快照。[scheduler.py:110](../src/local_tts/server/scheduler.py#L110)、[app.py:119](../src/local_tts/server/app.py#L119) |
   | #3 TTL／janitor | worker 清除工作引用，獨立 janitor 可在長工作期間清理；記憶體補驗確認過期工作與音訊 weakref 均釋放。[scheduler.py:61](../src/local_tts/server/scheduler.py#L61)、[scheduler.py:145](../src/local_tts/server/scheduler.py#L145) |
   | #4 生成預算 | 設定值確實成為硬上限，低於 125 的設定也有效。[engine.py:125](../src/local_tts/engine.py#L125) |
   | #5 停頓驗證 | 非有限值、單次超過 60 秒、總量超過 600 秒均在入列前拒絕；邊界值可接受。[app.py:110](../src/local_tts/server/app.py#L110) |
   | #7 其他報告修正 | 卸載至再次入列為 **0.983 秒**，重新載入 **5.6 秒**；ASR 差異描述已收斂。[server.log:627](../outputs/step2/final/server.log#L627)、[step2-report.md:45](../docs/step2-report.md#L45) |

6. **【備註】實測主要數字與 final 原始紀錄一致。**  
   四組長文時長／耗時／RTF、ASR、六組說話人數字均吻合；並行為 **4 接受、2 拒絕、4 成功且 FIFO**；強制截斷 **3 次後成功生成 27.12 秒**。final 的 **12 個音檔**均重新完整解碼成功，全部為 48 kHz 立體聲。證據：[long_results.json:15](../outputs/step2/final/long_results.json#L15)、[concurrency.json:215](../outputs/step2/final/concurrency.json#L215)、[truncation.json:2](../outputs/step2/final/truncation.json#L2)、[ffprobe.txt:1](../outputs/step2/final/ffprobe.txt#L1)。

7. **【備註】測試受唯讀環境限制。**  
   指定 pytest 指令在建立擷取暫存檔時中止；停用擷取後為 **14 passed、6 個 fixture 初始化錯誤**。未能完整執行的六項為：佇列／FIFO／未完成下載、同步逾時、閒置重載、載入失敗釋放、停頓驗證、失敗下載競態，皆卡在共用的 [test_server.py:59](../tests/test_server.py#L59) `tmp_path`。另以記憶體直接端點補驗，停頓驗證及 queued／failed 音訊回應的核心流程通過。