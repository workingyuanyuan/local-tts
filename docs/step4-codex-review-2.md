第一輪 15 項核對結果如下。

| 項目 | 狀態 | 核對結果 |
|---|---|---|
| 1 安裝器標記安全 | 已修正 | `scripts/install_agent_rules.sh:23` 驗證唯一完整標記，並以暫存檔原子替換。 |
| 2 工作目錄刪除 | 已修正 | `scripts/step4_run.sh:17` 加入任務白名單，拒絕未標記的非空工作目錄。 |
| 3 終止失敗仍輪詢 | 已修正 | `client.py:147` 加入 failed 狀態；MCP 改為重新生成，CLI 不再附 fetch。 |
| 4 恢復時保留路徑 | 部分修正 | 錯誤恢復已要求路徑，pending 分支仍省略；見下方。 |
| 5 輸入與 pause 上限 | 已修正 | Claude 規則 `SKILL.md:49`、Codex 規則第 9 行已列限制及分批方式。 |
| 6 CLI 有限等待 | 已修正 | Claude 規則第 21 行、Codex 規則第 5 行已加入期限及 exit 3 處理。 |
| 7 解析失敗仍 PASS | 部分修正 | checker 例外與缺少 ASR 已處理；transcript 解析仍有漏洞。 |
| 8 錯誤恢復後遺失原文 | 已修正 | `step4_evidence.py:56` 從錯誤保存 job／原文關聯，checker 要求完成檔案有原文。 |
| 9 完成路徑與受驗檔案無關 | 已修正 | `step4_check.py:72` 逐檔核對位置、解碼、原文與副本雜湊。 |
| 10 影片驗收不足 | 部分修正 | 已加入成片 ASR 與長度檢查，仍缺場景數及逐段對齊斷言。 |
| 11 反例漏算生成嘗試 | 部分修正 | 已計失敗、未完成與 shell 呼叫，但合法 CLI 語法仍可漏算。 |
| 12 ASR 漏句判定過寬 | 部分修正 | 已檢查 deletions，但短漏字仍可直接 PASS。 |
| 13 OOM 原因推測 | 已修正 | Claude 規則第 80 行改為先查狀態、GPU 與日誌。 |
| 14 音色一致性保證 | 已修正 | Claude 規則第 37 行改為盡力維持，說明參考音訊門檻。 |
| 15 平行執行與數字範例 | 已修正 | `step4_all.sh:9` 平行啟動並保存退出碼；報告已改為 Point two／Point 2。 |

仍有以下發現：

1. **應修正｜[scripts/asr_compare.py:133](../scripts/asr_compare.py#L133)**  
   檔尾過濾會整段刪除轉寫。時間戳超出音檔長度，不能證明整段文字都是幻覺：若一段包含真實額外語音，但結束時間被 ASR 高估，也會被移除而隱藏缺陷；若包含正常尾句，則可能誤判漏句。  
   **修正：**保存原始段落與時間戳，將此類段落標為待確認，以檔尾裁切重轉寫或聆聽判定，避免直接丟棄。

2. **應修正｜[scripts/asr_compare.py:149](../scripts/asr_compare.py#L149)**  
   第二輪的刪除結果與第一輪的插入結果被混合，可能組出兩輪都未支持的 PASS。已用現有比較函式重現：第一輪少了 `off site`；第二輪完整辨識原文，另多出 `Never keep your backups`；合併後三項異常全部為空，WER 達 0.8 仍通過。第一輪單獨產生的插入幻覺，也無法由第二輪清除，可能誤報失敗。  
   **修正：**完整保存兩輪結果；任一輪出現插入或兩輪對缺漏有分歧時，要求局部確認，不能只依「較佳」分數消除異常。

3. **應修正｜[scripts/asr_compare.py:106](../scripts/asr_compare.py#L106)、[scripts/step4_check.py:135](../scripts/step4_check.py#L135)**  
   英文漏一字、增加三字以內，以及中文短缺漏仍可能直接通過。已重現 `Do not delete the backup.` 變成 `Do delete the backup.`，WER 0.2，但所有異常欄位皆空。這尚不足以支持「無漏字、無截斷」。  
   **修正：**將所有未匹配片段列為候選異常，包含短尾句與單字；ASR 誤差須經局部重轉寫或人工確認並保存結論。

4. **應修正｜[Claude SKILL.md:55](../agent-rules/claude/narration/SKILL.md#L55)、[Codex 規則:10](../agent-rules/codex/AGENTS.local-tts.md#L10)**  
   pending 分支仍教 `get_speech_job(job_id)`。MCP 重啟後路徑記憶消失，依此操作仍會寫到專案外的預設目錄。  
   **修正：**所有 pending 查詢與恢復一律帶原始絕對 `output_path`，同步更新已安裝副本及 MCP 指引。

5. **應修正｜[src/local_tts/mcp_server.py:73](../src/local_tts/mcp_server.py#L73)**  
   `unknown_job` 也收到「工作可能仍執行，繼續 get，避免重送」的 recovery，與規則的重新生成指示衝突，會形成無效輪詢。  
   **修正：**在通用恢復分支前處理 `unknown_job`，明確要求重新生成。

6. **應修正｜[scripts/step4_evidence.py:105](../scripts/step4_evidence.py#L105)**  
   Codex 解析器跳過所有非 JSON 行；整份內容只有 `not json` 時，仍成功回傳零呼叫摘要。Claude 的 `{}` 也被接受。反例因此可能將無效紀錄當成「沒有呼叫」。第 27 行的工具結果解析同樣會把錯誤吞成 `{}`。  
   **修正：**驗證事件結構及必要的開始／完成事件；生成結果解析失敗須使驗收失敗。

7. **應修正｜[scripts/step4_evidence.py:24](../scripts/step4_evidence.py#L24)**  
   shell 偵測漏掉合法的 `tts --json say ...`、`tts --quiet say ...`。若輸出到任務目錄及預設目錄之外，反例檢查仍可通過。  
   **修正：**解析 CLI 參數並支援子命令前的共用選項；以服務端請求紀錄補強「沒有生成請求」的斷言。

8. **應修正｜[scripts/step4_check.py:100](../scripts/step4_check.py#L100)**  
   三個音檔不等於三個畫面。單一靜態畫面搭配完整旁白，仍能通過目前檢查。  
   **修正：**驗證成片場景數及切點，逐一比對旁白累計時長；或保存明確的人工驗收紀錄。

引擎方面，`max_retries=2` 確實最多生成三次，失敗會回傳 `segment_failed` 與原始、從零起算的段落 index；切細後仍保留原 index。假音訊改為一秒符合測試用途。日誌可核對八份、六十八段校準資料，全部 attempt 0；13.7／6.4 秒的樣本與 17.08 秒門檻吻合，支持這批中英文樣本沒有誤拒。

- **建議｜[src/local_tts/engine.py:183](../src/local_tts/engine.py#L183)**：校準尚不能證明所有合法輸入都安全。二十位數字串估時只有 0.34 秒，門檻 3.748 秒，合理的逐位朗讀可能被拒絕。應改善數字／縮寫估時，補上門檻邊界與正常慢速樣本測試。
- **建議｜[src/local_tts/engine.py:22](../src/local_tts/engine.py#L22)、[tests/test_engine.py:161](../tests/test_engine.py#L161)**：仍寫成第四步觀察到真正持續發聲，與報告更正矛盾；應改為防禦性檢查。

已安裝 Claude skill 與原稿逐位元組相同；Codex marked block 唯一完整且與原稿相同。兩套權限設定符合報告。

第四輪證據支持六任務成功、兩個退出碼為零、平行執行、Codex reading 一次生成後兩次接續，以及十筆第一輪 ASR 結果。八個音檔與兩部成片均可解碼，副本雜湊相符。實際場景切點也符合旁白：Claude 為 9.50／19.10 秒，Codex 為 7.36／13.55 秒。

- **建議｜[docs/step4-report.md:109](../docs/step4-report.md#L109)**：`final/` 未保存局部重轉寫、未過濾時間戳、插入語音實驗及十檔重跑兩次的原始結果，無法逐項重查這些聲稱。應補存輸入雜湊、參數、原始轉寫與確認結論。

需先修正