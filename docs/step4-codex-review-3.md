第二輪核對結果如下。

| 項目 | 狀態 | 核對結果 |
|---|---|---|
| 1 檔尾過濾 | 部分修正 | 舊過濾已移除；[asr_compare.py:176](../scripts/asr_compare.py#L176) 仍可能產生空的裁切區間，見下方。 |
| 2 混合兩次轉寫形成 PASS | 部分修正 | 已改為局部重轉寫，但仍可能忽略剪段中新出現的異常。 |
| 3 短缺漏直接放行 | 部分修正 | 驗證集能抓到漏掉 `not`；單位插入、冠詞及裁切邊緣判定仍會掩蓋缺陷。 |
| 4 pending 保留原始路徑 | 已修正 | [mcp_server.py:92](../src/local_tts/mcp_server.py#L92) 回傳 `output_path`／`next`；規則與安裝副本均要求明確傳回路徑。 |
| 5 unknown_job 恢復 | 已修正 | [mcp_server.py:72](../src/local_tts/mcp_server.py#L72) 明確要求重新生成；測試涵蓋此分支。CLI 另有下述遺漏。 |
| 6 transcript／結果解析 | 已修正 | [step4_evidence.py:84](../scripts/step4_evidence.py#L84) 拒絕非 JSON、要求完成事件；不可解析的成功語音結果會使正例失敗。 |
| 7 CLI 共用選項偵測 | 已修正 | [step4_evidence.py:26](../scripts/step4_evidence.py#L26) 已辨識 `tts --json say`、`tts --quiet say`；反例也檢查服務入列紀錄。 |
| 8 場景與逐段對齊 | 已修正 | [step4_check.py:135](../scripts/step4_check.py#L135) 檢查切點數及旁白累計時長；兩部成片符合。 |
| 建議：數字估時與測試 | 部分修正 | 純數字已逐位計算並加入測試；縮寫、混合英數、門檻邊界及獨立正常慢速樣本仍未涵蓋。 |
| 建議：引擎與測試註解 | 已修正 | [engine.py:22](../src/local_tts/engine.py#L22) 改為防禦性檢查；測試註明模擬輸出。 |
| 建議：保存原始 ASR 證據 | 部分修正 | 已保存剪段文字、區間、結論與驗證集雜湊；仍缺原始時間戳及完整執行資訊。 |

規則原稿與已安裝副本一致。正例平行執行、兩組完成後才依序執行反例，符合報告。

仍有以下發現：

1. **應修正｜[scripts/asr_compare.py:193](../scripts/asr_compare.py#L193)、[同檔:195](../scripts/asr_compare.py#L195)**  
   `minor` 仍會直接放行真實缺陷。以注入轉寫重現：原文 `Do delete the backup.`，整檔及剪段都辨識成 `Do not delete the backup`，結果仍是 `resolved=true, minor=true, unresolved=[]`。英文冠詞即使兩次都確認缺漏，也直接通過。現有 15 項沒有測試這類實際短插入。  
   **修正：**`minor` 僅作分類；剪段仍確認存在的插入或缺漏，應保留為 unresolved，經進一步確認才能放行。加入多唸 `not`、中文多唸「不」及冠詞缺漏案例。

2. **應修正｜[scripts/asr_compare.py:185](../scripts/asr_compare.py#L185)**  
   `heard` 把所有 unequal replace 的較短部分也算成已聽到，且只憑 `b1 == 0` 就向右對齊。重現：原文 `Do not delete the backup.`，第一遍為 `Do delete the backup`，剪段為 `Now delete the backup`；`do not → now` 被算成已聽到 `not`，最終 PASS。這不符合報告所說的「同長度內容在相同位置」。  
   **修正：**unequal replace 不得靠 `min()` 補足 heard；邊緣對齊須有實際裁切與可靠錨點，否則保留待確認。加入此缺漏回歸案例。

3. **應修正｜[scripts/asr_compare.py:197](../scripts/asr_compare.py#L197)**  
   剪段只檢查第一遍候選的異常方向。重現：原文 `Do not delete the backup.`，第一遍漏 `not`，剪段為 `Do not permanently delete the backup`；補回 `not` 後直接 PASS，新出現的 `permanently` 完全未檢查。仍可能組出沒有任何一份轉寫完整支持的 PASS。  
   **修正：**確認候選時，同時檢查剪段可靠對齊範圍內的缺漏與插入；新異常須另列候選，保存後續確認結果。

4. **應修正｜[scripts/asr_compare.py:176](../scripts/asr_compare.py#L176)**  
   裁切起點沒有以上限限制。以十秒音訊、十二至十四秒的幻覺插入重現，產生 `window=[11.0,10.0]`，送入重轉寫的是零個樣本；若回傳空轉寫，插入便被標為 resolved。這沒有實際檢查檔尾音訊。  
   **修正：**保證 `0 ≤ t0 < t1 ≤ dur`；時間戳完全超界時改取有效檔尾窗口，保存原始超界時間戳。驗證集加入整個候選都在檔尾之後的情況。

5. **應修正｜[src/local_tts/mcp_server.py:114](../src/local_tts/mcp_server.py#L114)**  
   原生成使用 `overwrite=True` 並回 pending，照新 `next` 明確傳回相同路徑時，會失去覆寫設定：只有省略路徑才沿用 `entry.default_overwrite`。既有檔案因此回 `output_exists`，無法依提供的接續指令完成刻意重新生成。  
   **修正：**明確傳回相同目標時沿用原覆寫授權；`next`／恢復資訊也保留必要參數，以支援 MCP 重啟。補上既有檔案、pending、依 next 接續的測試。

6. **建議｜[src/local_tts/cli.py:109](../src/local_tts/cli.py#L109)、[README.md:56](../README.md#L56)**  
   `tts job` 取得 succeeded 後，若下載時服務重啟或工作過期，`unknown_job` 仍附無效 `fetch_command`；`cmd_say` 已排除此錯誤。README 也沒有完整交代此例外。  
   **修正：**統一可恢復錯誤判斷，兩個 CLI 分支都排除 `unknown_job`／終止失敗，並同步文件。

7. **建議｜[src/local_tts/mcp_server.py:97](../src/local_tts/mcp_server.py#L97)**  
   `next` 與 recovery 直接以單引號包住路徑；`/project/actor's scene.wav` 會產生無效呼叫字串。  
   **修正：**使用正確字串編碼，或回傳結構化工具名稱與參數；同步更新第 193 行仍省略路徑的工具說明。

8. **建議｜[src/local_tts/text.py:38](../src/local_tts/text.py#L38)、[tests/test_engine.py:176](../tests/test_engine.py#L176)**  
   數字修正只處理純數字 token；混合英數與縮寫仍可能低估。現有正常假音訊又依同一估時函式決定長度，無法驗證慢速合法語音是否被誤拒。  
   **修正：**加入混合英數估時、門檻兩側及長度獨立於估時函式的正常慢速樣本。

9. **建議｜[scripts/step4_evidence.py:26](../scripts/step4_evidence.py#L26)**  
   `tts health`、`tts models`、`tts voices` 尚未列入 shell 偵測，也不會留下入列或音檔紀錄，與報告「沒有任何 CLI 存取服務」的斷言範圍不一致。  
   **修正：**將這些合法子命令加入偵測。

10. **建議｜[scripts/asr_compare.py:209](../scripts/asr_compare.py#L209)、[docs/step4-report.md:168](../docs/step4-report.md#L168)**  
    原始 Whisper 字詞／段落時間戳仍未保存，final 也缺輸入雜湊及執行參數；歷史超界時間戳聲稱無法逐項重查。  
    **修正：**保存兩次原始轉寫與時間戳、來源／音訊雜湊、模型 revision、參數及確認結論。

第 5 輪證據支持報告中的六任務結果、三個零退出碼、工具次數、voice／language、pending 接續與影片切點。六份 transcript 重解析結果一致；八個音檔及兩部影片均完整解碼，十個副本雜湊吻合。十筆 ASR、唯一剪段候選及零 minor 的統計相符；驗證集雜湊與十五項預期亦相符。完成條件中的「無漏句、無截斷」仍受上述 ASR 放行漏洞影響。

需先修正