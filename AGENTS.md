# local-tts：Agent 專案規則

本機 TTS 服務，讓 Claude Code、Codex 等 coding agent 在任務需要旁白時，自行用本機 GPU 生成語音。第一版模型是 MOSS-TTS-Local-Transformer-v1.5。安裝與使用方式見 [README.md](README.md)；這份文件只寫修改本 repo 時必須遵守的規則。

## 架構

```
Agent ──MCP stdio──▶ tts-mcp ─┐
      ──shell──────▶ tts CLI ─┤  HTTP（回傳音訊 bytes 與中繼資料）
                              ▼
                    tts-server（常駐 FastAPI，systemd user service）
                      └─ 單一 GPU worker ← 有界佇列
                           └─ config/models.yaml 模型註冊表 → adapter
```

| 位置 | 內容 |
|---|---|
| `src/local_tts/server/` | FastAPI 端點（`app.py`）、排程與工作狀態（`scheduler.py`） |
| `src/local_tts/engine.py`、`text.py`、`audio.py` | 切段、時長估計、重試與截斷重切、拼接與編碼 |
| `src/local_tts/adapters/` | 模型 adapter（`moss_local.py`）；新模型就加新的 adapter |
| `src/local_tts/client.py` | CLI 與 MCP 共用的 HTTP client：寫檔、路徑、期限、重試 |
| `src/local_tts/cli.py`、`mcp_server.py` | `tts` CLI 與 `tts-mcp` |
| `config/` | 伺服器設定、模型與聲音註冊表 |
| `agent-rules/` | 給使用者的 Agent 的使用規則原稿，由 `scripts/install_agent_rules.sh` 安裝 |
| `tests/` | 單元測試（假 adapter，不需 GPU） |
| `scripts/` | 實測與驗收腳本（需要執行中的服務與 GPU） |
| `docs/` | 開發各步的實測報告與審查紀錄 |

## 不可破壞的契約

**排程**
- 只有一個服務程序、一個 GPU worker。請求進有界佇列；佇列滿時回 `queue_full`，未取走的音訊超過 `max_result_bytes` 時回 `results_full`，不無限堆積。
- 模型的載入與卸載受互斥鎖保護，同一模型不會重複載入。切換模型前先卸載目前的模型。
- 閒置卸載只在沒有執行中工作、且佇列為空時觸發。

**模型與聲音**
- 未指定 `model` 時用 `models.yaml` 的 `default`。
- 未指定 `voice` 時不使用任何已註冊的聲音。為了讓同一次呼叫的音色一致，以第一個至少 3 秒的合格片段作為後續片段的參考；在它之前較短的片段，會用這個參考重新生成。
- 指定 `voice` 時，每一段都用同一個參考音檔。
- 未知的模型或聲音要回錯，並列出可用項目。
- 不支援的參數一律明確報錯，絕不靜默忽略（HTTP 與 MCP 都一樣）。
- HTTP 的 OpenAI 相容範圍只有 `/v1/audio/speech` 的 `model`、`input`、`voice`、`response_format`（wav、flac、mp3）；其他擴充欄位要寫進文件。

**長文**
- 依句界切段，`[pause …]` 等標記不可拆開。
- 截斷（達到 frame 上限）時切細重試；重試次數有上限，最終失敗要回報是哪一段。
- 時長門檻是和專案擁有者約定的（`engine.py`）：
  - 漏唸：輸出為空，或估計超過 2 秒、且生成時長低於「估計 × 0.3 − 0.5 秒」。重試，最終報錯。
  - 疑似漏句：估計超過 2 秒、且低於「估計 × 0.7」（沒有寬限）。重試；仍偏短時保留最長的一次，並在 `warnings` 註明。
  - 持續發聲：高於「估計 × 2.2 + 3 秒」，代表模型唸完後還在發聲。重試，最終報錯。
- 句中少幾個字的漏唸是模型問題，**不要**在服務端加 ASR 或其他防護。

**檔案與工作**
- 服務只回傳音訊，不寫入呼叫端的檔案；由 tts-mcp 或 CLI 寫到呼叫端指定的**絕對路徑**。
- 目標已存在時預設報錯，`overwrite=true` 才覆寫。未指定路徑時寫到 `~/.local/share/local-tts/output/`。Windows 路徑只在 WSL 下用 `wslpath` 轉換。
- 工作建立後，每個錯誤都要帶 `job_id` 與接續方式，永遠不要自動重送相同的文字。
- 送出一律帶 `Idempotency-Key`。
- 一次呼叫的等待請求（送出、輪詢、健康查詢）共用同一個期限，期限後最多 0.5 秒的寬限。完成音訊的下載（最多 30 秒）與本機寫檔不在此限。

## 模型與相依套件

- **不要改 `n_vq_for_inference`**。
- 注意力用 SDPA，照官方設定關閉 cuDNN SDPA（`enable_cudnn_sdp(False)`），flash、mem_efficient、math 保持開啟。不裝 flash-attn。
- `codec_weight_dtype` 必須明確設定：HF 上的程式碼預設是 bf16，GitHub 上的是 fp32。
- 模型與 tokenizer 的 HF revision 固定在 `config/models.yaml`。權重放在 HF 預設快取（`~/.cache/huggingface/`），不放進 repo。
- MOSS-TTS 是固定 commit 的 submodule（`third_party/MOSS-TTS`）。套件由 `uv.lock` 鎖定：改相依要用 `uv lock`，安裝用 `uv sync --frozen`。
- 官方取樣參數寫在 `models.yaml`，沒有實測依據不要改。

## 可攜性

這個 repo 要能在別台機器上使用，不只是開發者的筆電。
- 程式、設定、腳本、規則與文件中不寫死任何本機路徑、使用者名稱或硬體。路徑一律由檔案自身位置推導、用 `$PWD`，或由安裝腳本代入 `{{LOCAL_TTS_ROOT}}`（見 `agent-rules/` 與 `deploy/local-tts.service`）。
- 支援的環境：Linux x86_64（原生或 WSL2），加上 NVIDIA GPU 與支援 CUDA 12.8 的驅動。只在特定環境才有的功能，在其他環境要給明確的錯誤，不能直接崩潰：例如 WSL 才有的 `wslpath`、systemd，或沒有 GPU 時。
- 不要假設 GPU 的型號或顯存大小；需要的資源寫在 README 的需求段落。

## 開發與測試

- 用 uv 管理的 Python 3.12 venv（`.venv/`），不要動系統 Python，執行時用 `.venv/bin/python`。
- 單元測試：`.venv/bin/python -m pytest -q tests`（約 20 秒，不需 GPU）。修 bug 時要補回歸測試。計時相關的斷言要計算請求次數，或留足寬鬆的上限，否則會因為機器忙碌而不穩定。
- `scripts/step*_run.sh` 是使用 GPU 的實測，產物寫在 `outputs/`（不進 repo）：
  - 第 1 步：自己載入模型，必須先停止服務或確認它已卸載，以免兩份模型搶顯存。
  - 第 2 步：會自行停止並重新啟動 systemd 服務。
  - 第 3、4 步：需要服務正在執行。
- 改了 `config/*.yaml` 或伺服器程式後，要重啟服務（`systemctl --user restart local-tts`）才會生效。改了 `agent-rules/` 後要重新執行 `scripts/install_agent_rules.sh`。
- 結束程序要用確切的 PID，**不要用 `pkill -f`**：它可能比對到自己的 shell。

## 文件

- README 用繁體中文撰寫，內容要和程式一致：CLI 參數、MCP 工具、HTTP 端點、錯誤碼與設定。
- `docs/` 是各步當時的實測與審查紀錄，不改寫其中的結果；新的實測或審查另寫一份新的報告。
