# 第 4 步報告：Agent 使用規則

## 實作

| 檔案 | 內容 |
|---|---|
| `agent-rules/claude/narration/SKILL.md` | Claude Code user-level skill 原稿：description 列出觸發情境與中英文關鍵字，也寫明哪些情況不觸發 |
| `agent-rules/codex/AGENTS.local-tts.md` | Codex 規則原稿：同一套規則的精簡版，因為 `~/.codex/AGENTS.md` 對所有 Codex 工作都會載入 |
| `scripts/install_agent_rules.sh` | 安裝 skill，並更新 `AGENTS.md` 中以 `<!-- local-tts:begin/end -->` 標記的區塊（細節見下方） |
| `scripts/step4_all.sh`、`scripts/step4_run.sh` | 驗收：以 headless 模式讓兩個 Agent 執行實際任務。`STEP4_RECHECK=1` 可只對既有產物重跑檢查 |
| `scripts/step4_evidence.py` | 從 Claude stream-json／Codex `--json` 事件解析 local-tts 的使用情形（細節見下方） |
| `scripts/step4_check.py` | 所有驗收斷言 |
| `scripts/asr_compare.py`、`scripts/asr_compare_selftest.py`、`scripts/asr_validation.sh` | ASR 比對、以注入的轉寫做自我測試，以及用已知缺陷與正常音檔驗證比對結果 |
| `scripts/soundtrack_selftest.py` | 影片音軌比對的自我測試 |

`install_agent_rules.sh` 的行為：
- 可重複執行，只改 `AGENTS.md` 標記區塊內的內容，其餘不動；
- 標記不是「零組」或「唯一一組且順序正確」時拒絕修改；
- 以原子替換寫檔；
- 第一次修改前備份為 `AGENTS.md.bak-local-tts`。

`step4_evidence.py` 記錄的內容：
- 每次工具嘗試，包含失敗與未完成的；
- 每個完成檔案實際使用的文字：會跨越 `pending` 與帶 `job_id` 的錯誤，追蹤到同一個工作；
- 以 CLI（任何子命令）或 HTTP 存取服務的 shell 指令；
- 無法解析的語音結果。

transcript 有非 JSON 行、缺少完成事件，或 `message` 不是物件以外的結構錯誤時，以非零狀態結束。

### 規則內容
- **觸發情境**：影片、簡報、示範的旁白／配音，教材、文章朗讀，有聲內容，腳本試聽。只要文字稿，或指定其他 TTS 時不觸發。
- **為耳朵寫稿**：數字、單位、縮寫的讀法；`[pause]`；用拼音修正讀錯的中文詞；稿子存在音檔旁邊。
- **模型與聲音**：
  - 不指定 model，一律帶 language。
  - 未指定 voice 時，每次呼叫的音色不同；單次呼叫內會盡量沿用第一段 ≥ 3 秒的輸出。因此多段或分批需要同一位旁白者時，固定同一個 voice。
  - 註冊使用者自己的參考音檔要先徵求同意。
- **輸出位置**：絕對路徑，放在任務專案目錄並依該專案的結構；依用途選格式；不擅自覆寫。
- **切分方式**：
  - 影片或簡報每個場景一檔，用回傳的時長對齊畫面；連續朗讀一次送出。
  - 單次上限 20,000 字；每個 `[pause]` ≤ 60 秒，合計 ≤ 600 秒。超過時依段落分批，固定 voice，依序生成後合併。
- **長文與中斷**：
  - `pending` 時用結果中的 `next.arguments`（job_id、原始的絕對 `output_path`，有設定時再加上 `overwrite`）呼叫 `get_speech_job`，持續到完成。路徑一律要帶，因為 MCP 重啟後預設位置在專案之外；不重送。
  - 錯誤附 `job_id` 時依 `recovery` 處理：呼叫中斷就照它給的參數接續；`job_status: "failed"` 或 `unknown_job` 則修正原因後重新送出。
- **CLI 備援**：一律帶 `--timeout 45`；exit 3 時改跑回傳的 `fetch_command` 接續，不重跑 `tts say`。
- **交接與回報**：
  - 用 `duration_seconds` 對齊畫面；附 ffmpeg 合成範例，並提醒 `-shortest` 會切掉語音；用 ffprobe 驗證成品。
  - 回報路徑、時長與使用的聲音。
- **錯誤處理**：依實際錯誤碼說明處理方式，並寫出日誌位置。`gpu_out_of_memory` 時先查 `tts_status`、`nvidia-smi` 與日誌，不預設原因。

## 本步驟的程式修正

- **工作的終止與接續**：
  - 失敗的工作在錯誤中帶 `job_status: "failed"`。這種錯誤和 `unknown_job`（服務重啟或工作過期）都由 `client.resumable()` 判定為不可接續：MCP 的 `recovery` 要求重新送出，CLI 的 `tts say` 與 `tts job` 都不附 `fetch_command`。
  - 可接續的錯誤，`recovery` 以 JSON 給出 `get_speech_job` 的參數，路徑含引號也安全。
  - `pending` 結果附 `output_path` 與結構化的 `next`：`{"tool": "get_speech_job", "arguments": {...}}`。
  - 明確傳回 `generate_speech` 的同一路徑時，沿用它的 `overwrite` 設定，`next` 也會帶上，所以照著 `next` 就能完成刻意的重新生成，MCP 重啟後也一樣。
- **段落過長時重試**（防禦性檢查）：
  - `server.yaml` 的註解原本就寫「too short / too long」，但程式只實作了「過短」。現在補上「輸出長度 > 2.2× 估計 + 3 秒」也重試，超過重試上限回報 `segment_failed`。每段的實際長度與估計值都寫進日誌。
  - 校準：68 段真實輸出中，正常語音最多為估計的 2.14 倍；8 輪驗收都沒有觸發重試。
  - 至今**沒有觀察到真正的持續發聲**。第 3 輪我曾以為看到，結果是 Whisper 的幻覺。
- **時長估計**：數字串每位數各算一個詞；混合英數（`RTX5090`）拆成字母與數字分別估計；全大寫的縮寫（≤ 6 字母）逐字母估計，但整段大多是大寫時（例如 `THE QUICK BROWN FOX`）視為一般單字。
- **測試**：新增 16 個測試，涵蓋：
  - 失敗工作與 `unknown_job` 不可接續（MCP 與 CLI）；
  - `pending` 的 `next`、照 `next` 完成重新生成（含 MCP 重啟）、路徑含引號；
  - 過長重試與持續過長、門檻兩側；
  - 4 個以實際朗讀長度寫定的正常樣本，必須不重試，包括慢速中文與全大寫英文；
  - 數字、混合英數、縮寫與全大寫句子的估計。

  假 adapter 的音訊從 4 秒改為 1 秒，否則以短文字的估計，4 秒本身就會被判為過長。共 68 個測試通過。

## 權限（依你的決定開啟）

- `~/.codex/config.toml` 的 `[mcp_servers.local-tts]`：
  - 加上 `default_tools_approval_mode = "approve"`；
  - 註解中的上限修正為 240 秒，與程式一致。
- `~/.claude/settings.json` 的 `permissions.allow` 加入 `mcp__local-tts`。
- 驗收時 Codex 使用 `--strict-config`，codex-cli 0.163.0 接受這個設定鍵。
- Claude 只額外允許任務本身用到的 Bash 與 Skill，local-tts 的權限來自 settings。

## 驗收

`scripts/step4_all.sh <工作目錄> outputs/step4/final` 分兩個階段：
1. **正例**：Claude Code 與 Codex 的正例同時執行，TTS 請求共用 GPU 佇列。
2. **反例**：兩個反例在沒有其他工作時依序執行，這樣服務日誌才能證明期間沒有工作入列。

測試條件：
- prompt 都沒有提到 TTS、MCP 或工具名稱；
- 工作目錄在本專案之外，且必須是新目錄或此腳本建立過的目錄；
- 任務名稱以白名單驗證；
- 每個任務的起訖時間與 exit code 存在 `<任務>.run`。

### 斷言（`scripts/step4_check.py`）

transcript 或檢查程式失敗、輸出無法解析，或缺少預期的 ASR 結果，一律判 FAIL。

| 類別 | 條件 |
|---|---|
| 正例 | Agent exit 0；至少一次 `generate_speech`；沒有無法解析的語音結果。每個 `completed` 檔案都必須：解析後位於任務目錄內、存在、ffmpeg 全檔解碼無錯誤、有對應的原文，且副本的 sha256 與原檔相同 |
| 影片 | 有畫面與音軌；音軌長 19.5–30.5 s；畫面不比語音先結束；至少 3 段旁白 |
| 影片音軌 | 音軌必須就是各段旁白，不重疊，段外只有靜音：<br>• 每段在整條音軌中以互相關定位，段落順序取自它們在音軌中的位置，不依檔名<br>• 每個 100 ms 語音窗（片段 > −40 dBFS）的相關係數須 ≥ 0.4、能量差 ≤ ±3 dB；片段中的停頓窗在成片中也必須 ≤ −40 dBFS<br>• 段外每個 50 ms 窗（含最後不足一窗的部分）≤ −40 dBFS |
| 影片場景 | 場景變換數 = 段數 − 1；第 k 個變換落在第 k 段語音結束與第 k+1 段開始之間（±0.5 s）。偵測用 scene score > 0.002；1 秒內的多次偵測合併；影片頭尾 0.5 秒內的淡入淡出不計 |
| 反例 | 沒有任何 local-tts 工具嘗試；沒有以 CLI／HTTP 存取服務；服務日誌在任務的起訖時間內沒有工作入列；任務期間，任務目錄與預設輸出目錄都沒有新音檔；有 `script.md`。第一次檢查會把服務日誌與新音檔清單存進 `<任務>.negative.json`，重檢時沿用 |
| ASR | 每段音檔與 Agent 實際送出的文字比對，覆寫過的檔案用最後一次的文字。音檔與原文目前的 sha256 必須與 ASR 結果相同，否則判為過期。`unresolved` 必須為空，或每一項都有人聽過，並以 ok 記錄在 `asr_review.tsv`，最後一欄要寫明誰聽過、聽到什麼。程式不自動豁免任何項目 |

成品影片的整段 ASR 仍會執行，但只列為 INFO 參考：每段內容已經各自通過 ASR，音軌又在訊號層確認就是那些片段。改成這樣的原因是第 6 輪 `explainer.mp4`：同一段音訊單獨轉寫為「copies your」，在整支影片中卻被轉寫為「copies of your」（「your」被壓縮到 60 毫秒），是換了上下文造成的 ASR 誤差。

門檻依據：
- **語音窗**：第 5–8 輪 8 支影片共 29 段，每個語音窗的相關係數 ≥ 0.63、能量差在 ±0.3 dB 內。字被靜音時能量差 < −20 dB；換成別的語音則相關係數接近 0。整段 SNR 只作參考，因為它會掩蓋局部漏字。
- **場景**：靜態卡片的幀間雜訊約 2e-5。顏色相近的切換分數 ≥ 0.03；白底只換一個短標題約 0.0098。

`scripts/soundtrack_selftest.py outputs/step4/soundtrack_validation` 的 7 項全部符合預期：
- 正確的音軌通過；
- 換掉一段、結尾多接一段語音、中間 0.26 秒（一個字）被靜音、結尾多出 25 ms 的聲音、在片段的停頓中塞入 0.4 秒別的語音，都判失敗；
- 白底只換短標題的三張卡片，偵測到 7／14 s 兩個場景變換。

### ASR 比對（`scripts/asr_compare.py`）

**候選**：第一次轉寫（含字詞時間戳記）與原文之間，有刪除、插入，或長度不同的替換的地方，都列為候選。

**剪段確認**：每個候選依時間戳記剪出一段音訊，前後各加 1 秒，且至少 2 秒、位於檔案內（時間戳記超出檔尾時改取檔尾 2 秒），單獨重新轉寫後與附近原文對齊。三個條件都成立才算解決：
- 候選的每個原文單位都有聽到：相符、同一位置有同長度的內容，或只差空格；
- 在第一個與最後一個可靠錨點之間，沒有其他刪除或插入。可靠錨點是中文 ≥ 3 字、英文 ≥ 2 詞的相符片段；
- 錨點之外沒有多出的語音，只容許「切口亂字」：最多 3 字／2 詞，且依剪段的字詞時間戳記，位在切過語音的剪段邊界 0.5 秒內。候選本身離邊界 1 秒，所以永遠不會被當成切口亂字；檔案真正的開頭或結尾也不容許。

**固定解碼**：Whisper 以 temperature 0 解碼，不做取樣重試，同一份音訊每次的轉寫都相同。驗證集連跑兩次，15 份轉寫與判定完全一致：兩次結果存在 `results.prev.jsonl` 與 `results.jsonl`，比較紀錄在 `comparison.txt`，之後每次執行都會自動比較。原本的取樣重試會讓第 8 輪 `lesson.mp3` 的 error rate 在 0.01 到 0.26 之間變動，判定則都相同：檔尾幻覺都經剪段確認排除。

**未解決的處理**：一律列為 `unresolved`，沒有自動豁免。同長度的替換（例如「逕流」被讀成「除流／豬流」這類錯讀，或 ASR 誤聽）與只差空格的寫法則計為 substitutions，因為這項檢查只看漏唸與多唸，不檢查讀音。

**輸出內容**：第一次轉寫與每個剪段的原始字詞時間戳記、音訊與原文的 sha256、模型 revision 與參數，以及每個候選的段內最大音量（`span_peak_dbfs`），供聆聽者參考。

**正規化**：
- 中英文數字統一寫法，英文支援 0–999；
- 英文連字號合併（by-product＝byproduct）；
- 數字與字母分開（48kHz）。

**自我測試**：`scripts/asr_compare_selftest.py` 有 15 個注入轉寫案例（加上 1 個檔尾窗口檢查），全部符合預期，包括審查中重現的漏洞：
- 多唸「not」或多唸「不」；
- 「do not」被聽成「now」；
- 剪段補回「not」卻多出「permanently」；
- 完整聽到的邊緣字之後多唸「do not」／「不要」；
- 冠詞缺漏；
- 候選落在檔尾之後；
- 只差空格；
- 第 8 輪的兩個真實案例。

**驗證集**：`scripts/asr_validation.sh outputs/step4/asr_validation` 共 15 項，結果與 sha256 都保存在該目錄。

| 預期 | 檔案 | 內容 |
|---|---|---|
| fail | `notdel`、`bu` | 「Do not delete」唸成「Do delete」；「不要刪除」漏了「不要」 |
| fail | `lesson_en_1` 配 `lesson_en_extra.txt` | 結尾整句沒唸 |
| fail | `runon_en`、`runon_zh` | 文字唸完後接上無關的語音 |
| pass | `lesson_en_*`、`long_*`（6 份） | 正常音檔，包括「48 kHz」 |
| pass | `round3_*_lesson`（2 份） | Whisper 在檔尾之後編出文字，剪段確認後排除 |
| review | `lesson_zh_1`、`lesson_zh_2` | 正常中文音檔，自動檢查無法排除：「第二，」聽成「第二道」、檔尾 2 秒剪段出現 YouTube 片尾幻覺、「先」聽成「烟」。必須被標記給人聽，藉此記錄誤報率 |

正常音檔中有 2/10 會被標記為需要人聽，這是不自動豁免的代價。

### 結果（第 8 輪，最終）

第 8 輪第一次執行有三項失敗。修正前兩項（都是檢查程式的錯誤）後，正例以 `STEP4_RECHECK=1` 重檢，不重跑 Agent；反例則重新執行，因為當時沒有記錄起始時間。Codex 第 4 輪審查後的檢查（逐窗音軌比對、切口時間、固定解碼、雜湊綁定、反例證據）也都以重檢套用到同一批產物。完整經過記錄在 `outputs/step4/final/recheck.txt`。

| 項目 | 原因 | 處理 |
|---|---|---|
| `claude_negative` 無法解析 | Claude stream-json 有 `message` 為字串的事件，解析器當掉（Agent 本身 exit 0） | 修正解析器；兩個反例重新執行，都 PASS |
| `codex_reading` 插入「第三水循環的能量」 | 檔尾 0.12 秒內的 Whisper 幻覺。剪段只聽到「…淡水不斷更新」，但剪段開頭的「流」被對到遠處的「逕流」 | 改用可靠錨點；重檢 PASS |
| `claude_video` 的 `scene03.wav`：「計時器一響」被聽成「記起祥」／「自己想」 | **真的 TTS 漏唸**：你在 2026-10-23 聆聽確認，見下方 | 保持 FAIL，聆聽結論記錄為 defect |

| 任務 | 結果 | Agent 的做法 |
|---|---|---|
| claude_video：番茄工作法短片 | 流程 PASS；scene03 ASR FAIL（確認為模型漏唸） | 自動載入 `narration` skill → `list_voices` → 3 段 `generate_speech`（都用 `demo_zh_female`、`zh`）。總長 30.12 秒略超過 30 秒，於是縮短第 3 段，並帶 `overwrite: true` 重新生成。畫面每段多 0.5 秒、加 0.4 秒淡入淡出；場景變換 9.75／19.92 s，落在各段語音之間；音軌 SNR 10.9／33.0／41.5 dB |
| claude_reading：英文教材 | PASS | 載入 skill，改寫成口語版，一次生成 `lesson.mp3`（`en`）。檔尾 0.1 秒內的 Whisper 幻覺（段內 −60 dBFS）經剪段排除 |
| codex_video：3-2-1 備份原則短片 | PASS | `list_voices` → 3 段（都用 `demo_en`）→ 依時長渲染畫面；場景變換 7.90／14.86 s，與語音切換點相同；音軌 SNR 25–28 dB |
| codex_reading：中文教材 | PASS | 改寫成口語版，一次生成 `lesson-narration.mp3`（`zh`） |
| claude_negative、codex_negative | PASS | 沒有接觸 TTS，服務日誌 0 個工作，只寫了 `script.md` |

**`scene03.wav` 漏唸**：
- 自動偵測的證據：
  - 第一次生成（原文多「走走、」三個字）是 11 秒多。只少三個字的第二次生成只有 7.27 秒，約為估計的 1.1 倍；中文正常約 1.5 倍。
  - 兩次獨立轉寫都把開頭五個字聽成另外三個音節。
- 你在 2026-10-23 聆聽確認開頭的「計時器一響」確實漏唸。判斷是 TTS 模型本身的偶發問題，屬於本專案無法控制的部分。
- 依你的決定，**服務端不加防護**。引擎的「過短」門檻（< 0.3× 估計）攔不到這種局部漏唸，是驗收的逐段 ASR 抓到的，也就是說檢查本身運作正確。
- 聆聽結論以 `defect` 記錄在 `outputs/step4/final/claude/asr_review.tsv`，這段因此維持 FAIL（`asr_verdict.txt`）。只有 `ok` 的結論才會放行。重新判定的指令：

  ```bash
  .venv/bin/python scripts/step4_check.py asr outputs/step4/final/claude/asr.jsonl outputs/step4/final/claude/asr_pairs.tsv outputs/step4/final/claude/asr_review.tsv
  ```

**紀錄位置**：2026-10-23 WSL 重啟清空了 scratchpad 中的工作目錄。ASR 紀錄已改為指向 `outputs/step4/final/<agent>/<task>/` 下的副本，每個檔案的 sha256 都與 ASR 當時記錄的相同；`step4_check.py` 之後也會直接對副本執行 ASR。`STEP4_RECHECK` 需要原工作目錄，所以這批紀錄已無法完整重檢，但 ASR 判定仍可重跑。經過記錄在 `recheck.txt`。

前幾輪的紀錄保留在 `outputs/step4/round1..7/`，只有 `final/` 是完整、自洽的紀錄。各輪 Agent 的行為都正確；失敗都來自斷言逐輪加嚴，以及上述的 ASR／解析問題。

### 實測中發現的 ASR 問題（整理）

1. **英文數字**：ASR 把「Point two」寫成「Point 2」，被誤判為漏句。已加入數字正規化。
2. **長檔跳字**：整段 ASR 少了「地下，成為」，剪段單獨轉寫後完整聽到。
3. **檔尾之後的幻覺**：英文檔出現 62.3–89 s 的「Number four, it happens in the comics…」，但檔案只有 62.4 s；中文檔則出現 YouTube 片尾的「請不吝點讚訂閱…」。
4. **換上下文後的誤差**：整支影片的 ASR 多出「of」，單段轉寫則沒有。
5. **審查重現的漏洞**：Codex 以注入轉寫重現了多項漏洞，包括「取較佳的一次」拼出的 PASS、自動豁免、`min()` 補足、剪段新增的異常、空窗口。現在都由自我測試涵蓋。

### 觀察

- **聲音與時長**：兩個 Agent 都為每個場景固定同一個 voice、每個場景一檔，並用回傳的時長決定畫面長度。
- **教材改寫**：兩個 Agent 都把教材改寫成適合聆聽的口語稿。
- **重新生成與接續**：Claude 為了符合長度限制而重新生成時，主動帶 `overwrite: true`；遇到 `pending` 時，兩個 Agent 都接續，沒有重送。
- **誠實回報**：Claude 的最終回報寫明「旁白我沒有實際聽過」。也正是這類沒人聽過的音檔，需要逐段 ASR 把關。

## 第一版完成條件對照

| 條件 | 狀態 |
|---|---|
| Claude Code 與 Codex 在需要旁白的任務中自行選用工具並生成音檔（實際製作有旁白的影片） | 本步驟驗收：兩個 Agent 的影片與教材任務流程都正確；只要文字稿時不觸發 |
| 多個同時請求排隊依序完成、不 OOM | 第 2 步 `concurrency.json`；本步驟兩個 Agent 也平行執行 |
| 長文無漏句、無截斷（ASR 比對） | 第 2 步；本步驟所有產物都經剪段確認的 ASR 比對。第 8 輪 `scene03.wav` 開頭的模型漏唸被 ASR 正確抓到，並經你聆聽確認；這屬於模型層面的偶發問題，依你的決定不加服務端防護 |
| 閒置卸載後再次請求正常 | 第 2 步 |
| 錯誤與逾時明確回報 | 第 2、3 步；本步驟再區分可接續錯誤與終止失敗 |
| 輸出能被 ffmpeg 讀取 | 第 2、3 步，以及本步驟的 mp4 合成、全檔解碼與音軌比對 |
