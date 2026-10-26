# local-tts

本地 TTS 服務：讓 Claude Code、Codex 等 Agent 依任務需求主動生成旁白語音。支援多個模型，由 Agent 或用戶指定；第一版只部署 MOSS-TTS-Local-Transformer-v1.5。

## 環境（已確認）

- WSL2 Ubuntu 26.04，ext4（/dev/sdd），62 GB RAM，磁碟充足；WSL2 已啟用 systemd 與否需確認
- GPU：RTX 5090 Laptop，24 GB，Blackwell sm_120，驅動 CUDA 13.4
- 系統 Python 3.14 → **不要動**；專案用 uv 建 Python 3.12 venv（`.venv/`）
- **系統尚未安裝 uv、pip、conda、ffmpeg** → 第 0 步先裝 uv 與 ffmpeg（含共享函式庫，torchcodec 需要）
- 不要把任何東西放在 `/mnt/c`（9P 很慢）

## 模型：MOSS-TTS-Local-Transformer-v1.5

- HF：`OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5`（約 4–5B，BF16，Apache-2.0）
- 需搭配 `OpenMOSS-Team/MOSS-Audio-Tokenizer-v2`（經 `processor.audio_tokenizer` 載入）
- 程式碼：https://github.com/OpenMOSS/MOSS-TTS，`pip install -e ".[torch-runtime]"`（cu128 index）
  - torch-runtime 鎖定：torch/torchaudio 2.9.1+cu128、torchcodec 0.8.1、transformers 5.0.0
- `AutoModel/AutoProcessor` + `trust_remote_code=True`；**不要改 `n_vq_for_inference`**
- 依官方範例初始化 SDPA：`enable_cudnn_sdp(False)`，flash/mem_efficient/math 保持開啟
- 官方建議取樣：`do_sample=True, audio_temperature=1.7, audio_top_p=0.8, audio_top_k=25, audio_repetition_penalty=1.0`
- 輸出 48 kHz 立體聲；31 語言；支援 `[pause 3.2s]`、拼音控制
- 聲音克隆：只需參考音檔；逐字稿為選用（Continuation 模式才需要）
- `max_new_tokens=4096` ≈ 328 秒音訊僅為理論上限，不是穩定長度保證，也不是推論耗時
- 第一版用 SDPA，不裝 flash-attn；SGLang-Omni 暫不採用（WSL2+Blackwell 風險高），日後可當另一個 adapter
- 權重放 HF 預設快取 `~/.cache/huggingface/`，不放進專案

## 架構

```
Agent ──MCP stdio──▶ tts-mcp ─┐
      ──shell──────▶ tts CLI ─┤  HTTP（回傳音訊 bytes + 中繼資料）
                              ▼
                    tts-server（常駐 FastAPI，systemd user service）
                      └─ GPU worker（單一）← 有界佇列
                           └─ models.yaml 模型註冊表 → adapter
```

### GPU 排程

- 單一服務程序、單一 GPU worker；請求進入有界佇列，滿了就明確回錯（不要無限堆積）
- 模型載入／卸載受互斥鎖保護；同一模型不會重複載入
- 閒置卸載只在「無執行中工作且佇列為空」時觸發
- 切換到另一模型時先卸載目前模型（24 GB 不保證能同時放兩個）

### 模型與聲音契約

- `model` 未指定 → 用 `models.yaml` 的 `default`；未知模型 → 錯誤並列出可用模型
- `voice` 未指定 → 不做克隆，直接生成（模型預設音色）；未知聲音 → 錯誤並列出可用聲音
- `list_models` 回傳：id、是否預設、支援語言、能力（clone、pause 標記、拼音…）、是否已載入
- `list_voices(model?)` 回傳：name、描述、語言、相容模型
- adapter 不支援的參數 → 明確報錯（不靜默忽略）
- HTTP「OpenAI 相容」第一版僅涵蓋 `/v1/audio/speech` 的 `model`、`input`、`voice`、`response_format`（wav、flac、mp3）；其餘擴充欄位（language、instructions 等）放在額外欄位並寫明
- 另有 `/health`（服務狀態、已載入模型、佇列長度）與 `/v1/models`

### 長文處理

- 依句界（中英標點）切段，不切斷 `[pause …]` 等標記；段間插入適當靜音後拼接
- 跨段一致性：有指定 voice 時每段使用同一參考音檔；未指定時以第一段輸出作為後續段的參考（實測後決定）
- 截斷偵測：某段生成達 `max_new_tokens` → 視為截斷，該段再切更細後重試
- 失敗重試有上限；最終失敗要回報是哪一段
- 逾時：先實測 RTF；若長旁白會超過 MCP/HTTP 等待期限，再加入工作 ID＋狀態查詢（避免 Agent 逾時後重複生成）

### 檔案寫入

- 服務只回傳音訊，不寫入呼叫端檔案；由 tts-mcp／CLI 寫到呼叫端指定的**絕對路徑**
- 回傳：檔案路徑、時長、取樣率、聲道數、使用的 model/voice
- 路徑若為 Windows 形式（`C:\...`）以 `wslpath` 轉換
- 目標已存在 → 預設報錯，需 `overwrite=true` 才覆寫
- 未指定路徑 → `~/.local/share/local-tts/output/<timestamp>.wav`

### Agent 使用規則（主動調用）

- Claude Code：user-level skill（`~/.claude/skills/narration/SKILL.md`）＋ MCP 以 user scope 註冊
- Codex：`~/.codex/config.toml` 註冊 MCP，`~/.codex/AGENTS.md` 加入使用規則
- 規則內容：觸發情境（影片旁白、簡報配音、教材朗讀…）、如何選模型與聲音、輸出放在任務專案目錄、產物交給後續流程（如影片合成）

## 可重現性

- 記錄實測成功的 MOSS-TTS commit、模型與 tokenizer 的 HF revision，在程式中固定 revision
- 使用 `uv.lock` 鎖定套件
- README 寫明：安裝、啟動／停止／重啟（systemd user service）、健康檢查、日誌位置

## 第一版步驟

0. 安裝 uv、ffmpeg；確認 WSL2 systemd 狀態
1. 建 Python 3.12 環境，安裝 MOSS-TTS，用官方範例完成中英文各一段的完整推論（含音訊編解碼）與一次聲音克隆；記錄顯存、RTF、commit／revision
2. 實作 tts-server（排程、註冊表、lazy load／idle unload、切段、端點）
3. 實作 tts-mcp 與 CLI，註冊到 Claude Code 與 Codex
4. 撰寫 Agent 使用規則（skill、AGENTS.md）

每一步完成後實測並回報結果，經用戶確認再進下一步。

## 第一版完成條件

- Claude Code 與 Codex 都能在「需要旁白」的任務中自行選用工具並生成音檔（以實際任務驗收，例如製作一段需要旁白的影片）
- 多個同時請求能排隊依序完成，不 OOM
- 長文無漏句、無截斷（以 ASR 回轉文字與原文比對驗證）
- 閒置卸載後再次請求可正常生成
- 錯誤（未知模型／聲音、佇列滿、截斷失敗）與逾時有明確回報
- 輸出音檔能被後續流程（如 ffmpeg）正常讀取
