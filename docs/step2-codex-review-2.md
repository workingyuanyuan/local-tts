**結論：需先修正。** 上一輪主要缺陷已修復，但下載端點仍有可重現的失敗競態，長文切分也仍有缺口。

1. **【阻擋】下載工作音訊時，完成失敗的競態仍會造成 500。**  
   端點先檢查錯誤，再檢查完成事件；若工作在兩次檢查之間失敗，就會把失敗工作送進 `audio_response()`，因 `meta` 為空而拋出 `KeyError('model')`，遺失原始結構化錯誤。已用 CPU 同步控制重現。應一致地讀取完成狀態、錯誤及結果。證據：[app.py:165](../src/local_tts/server/app.py#L165)、[app.py:113](../src/local_tts/server/app.py#L113)。

2. **【阻擋】支援的泰文長串仍無法切細。**  
   `_hard_split()` 把整串非 CJK、無空白文字當成單一單位。重現案例為 2,200 字元泰文、估計 102.34 秒；`split_text(text, 30)` 與 `split_finer(text)` 都只回傳原始一段，截斷後直接失敗。應提供長單位的切分退路。證據：[text.py:105](../src/local_tts/text.py#L105)、[engine.py:163](../src/local_tts/engine.py#L163)、[moss_local.py:22](../src/local_tts/adapters/moss_local.py#L22)。

3. **【建議】TTL 尚未完整釋放音訊，生成期間也不會每秒清理。**  
   worker 的區域變數 `job` 持續引用最後完成的工作；即使 `_purge()` 刪除字典紀錄，音訊仍存活。weakref 重現結果：TTL 過期、`jobs_count=0`，最後工作仍持有音訊。此外，清理與生成共用 worker，長工作執行期間不會清理。證據：[scheduler.py:115](../src/local_tts/server/scheduler.py#L115)、[scheduler.py:124](../src/local_tts/server/scheduler.py#L124)、[scheduler.py:133](../src/local_tts/server/scheduler.py#L133)。報告「worker 每秒清理」應修正：[step2-report.md:80](../docs/step2-report.md#L80)。

4. **【建議】生成預算會超過設定上限。**  
   `max(125, min(configured, frames))` 在設定 `max_new_tokens=100` 時仍傳入 `max_frames=125`，違反設定所述硬上限。證據：[engine.py:124](../src/local_tts/engine.py#L124)、[models.yaml:20](../config/models.yaml#L20)。

5. **【建議】停頓秒數缺少有限值與總量驗證。**  
   414 字元的輸入即可讓超長數字轉成 `inf`，配置靜音時拋出 `OverflowError`；很大的有限值也會直接配置大量記憶體。應在生成前驗證。證據：[text.py:146](../src/local_tts/text.py#L146)、[audio.py:33](../src/local_tts/audio.py#L33)。

6. **【建議】驗收腳本仍可能漏報失敗。**  
   `decode=FAIL` 不會令評估腳本失敗，`ffprobe` 退出碼也未檢查；卸載後請求未斷言 HTTP 200，強制截斷僅展示日誌、未斷言確實觸發。證據：[step2_eval.sh:22](../scripts/step2_eval.sh#L22)、[step2_run.sh:55](../scripts/step2_run.sh#L55)、[step2_run.sh:81](../scripts/step2_run.sh#L81)。

7. **【建議】報告有三處需更正。**

   - 顯存觀測最大值是 **13,038 MiB＝12.73 GiB＝13.67 GB**；CSV 有超過 120 秒的採樣空洞，應稱「採樣觀測最大值」。證據：[報告:35](../docs/step2-report.md#L35)、[CSV:199](../outputs/step2/final/nvidia-smi.csv#L199)、[CSV:919](../outputs/step2/final/nvidia-smi.csv#L919)。
   - 卸載完成至再次入列是 **0.899 秒**，報告寫成 7 秒；重新載入 6.0 秒與音訊 5.24 秒正確。證據：[報告:60](../docs/step2-report.md#L60)、[server.log:638](../outputs/step2/final/server.log#L638)、[server.log:640](../outputs/step2/final/server.log#L640)。
   - 「剩餘差異為個別同音字」過度概括；還有數字正規化差異、`assistants→assistance`、`過去→或許`，且轉錄差異無法直接歸因於 TTS。證據：[報告:36](../docs/step2-report.md#L36)、[asr_en.jsonl:1](../outputs/step2/final/asr_en.jsonl#L1)、[asr_zh.jsonl:2](../outputs/step2/final/asr_zh.jsonl#L2)。

8. **【備註】上一輪逐項核實結果。**

   | 上輪項目 | 核實結果 |
   |---|---|
   | #1、#3、#4 | Tensor 參考、中文長引文、重切停頓及子段參考串接已修正；相關測試通過。[test_engine.py:113](../tests/test_engine.py#L113) |
   | #2 | 部分載入物件立即存入實例；錯誤無原始例外鏈。模擬模型搬移失敗後，processor／model weakref 均釋放。[moss_local.py:60](../src/local_tts/adapters/moss_local.py#L60)、[engine.py:59](../src/local_tts/engine.py#L59) |
   | #5 | 參數衝突已修正，未完成工作可產生 409；仍有本輪第 1 項競態。[errors.py:7](../src/local_tts/errors.py#L7) |
   | #6 | 出列與 running 轉態、工作快照、閒置卸載與 submit 的互斥成立。[scheduler.py:90](../src/local_tts/server/scheduler.py#L90)、[scheduler.py:120](../src/local_tts/server/scheduler.py#L120) |
   | #7 | 同步逾時與工作 ID 已落實；TTL 尚有第 3 項缺口。[app.py:147](../src/local_tts/server/app.py#L147) |
   | #8 | 原始紀錄及並行斷言已補齊；驗收腳本仍有第 6 項缺口。 |
   | #9 | 短句漏句偵測、說話人評估範圍、主要套件與模型 revision 已改善；ASR 描述仍需收斂。 |

9. **【備註】實測主要數字吻合；完整測試受環境限制。**  
   四組長文時長／耗時／RTF、ASR 比率及六組說話人數字均吻合；並行為 4 接受、2 拒絕、4 成功且 FIFO；強制截斷 3 次後成功生成 27.12 秒；final 的 12 個音檔均可完整解碼。證據：[long_results.json:15](../outputs/step2/final/long_results.json#L15)、[concurrency.json:215](../outputs/step2/final/concurrency.json#L215)、[truncation.json:2](../outputs/step2/final/truncation.json#L2)、[ffprobe.txt:1](../outputs/step2/final/ffprobe.txt#L1)。

   指定 pytest 指令因唯讀沙箱無法建立暫存檔而未完成；停用擷取與快取後為 **12 passed、4 個暫存目錄初始化錯誤**。4 項 API 測試另完成直接端點的核心流程補驗；完整 HTTP／TestClient 測試仍待執行。相關初始化位置：[test_server.py:59](../tests/test_server.py#L59)。