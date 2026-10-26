**結論：需先修正。** 中英文推論、聲音克隆與音檔格式已通過基本檢查；但 codec 精度的實際載入設定與報告矛盾，應先對齊，才能把顯存與 RTF 當作第 2 步的基準。

1. **【阻擋】報告把 BF16 codec 記為 FP32，影響顯存基準的可信度。**  
   [腳本:44](../scripts/step1_smoke.py#L44) 從 HF 快照載入 processor，未指定 codec 精度；該快照的 [processor:216](~/.cache/huggingface/hub/models--OpenMOSS-Team--MOSS-TTS-Local-Transformer-v1.5/snapshots/be7766a6735b98bd793f7c79fb720b4d0f5d13b8/processing_moss_tts.py:216) 預設為 `bf16`，而 Git checkout 的 [processor:216](../third_party/MOSS-TTS/moss_tts_local_v1.5/processing_moss_tts.py#L216) 預設為 `fp32`。codec 會實際套用此設定：encoder／decoder 轉為指定精度，quantizer 保持 FP32。  
   因此，[報告:32](../docs/step1-report.md#L32) 的「codec（fp32）4.0 GiB」不成立。唯讀解析權重標頭得到：全 FP32 約 **7.91 GiB**，encoder／decoder BF16 加 quantizer FP32 約 **3.96 GiB**，後者才吻合紀錄。  
   **處理要求：**明確固定 codec 權重與計算精度，記錄 `get_codec_dtype_summary()`，對齊報告。若改採 FP32，需重測顯存、RTF 與輸出。

2. **【建議】補齊第 0 步的 systemd 狀態紀錄。**  
   [要求:86](../CLAUDE.md#L86) 明定確認 systemd，但報告未記載。此次可核實 [/etc/wsl.conf:2](/etc/wsl.conf:2) 為 `systemd=true`；`journalctl --no-pager -b -u user@1000.service -n 8` 亦顯示：
   ```text
   systemd[383]: Started dbus.service - D-Bus User Message Bus.
   ```
   這支持本次開機已有 user manager 運作。仍建議補上宿主環境的 `systemctl is-system-running`、`systemctl --user is-system-running` 輸出及異常單元說明。本次沙箱連接 bus 回傳 `Operation not permitted`，不能據此判定宿主 systemd 故障。

3. **【建議】`uv.lock` 延至第 2 步可接受，但應列為該步的首項工作。**  
   [報告:18](../docs/step1-report.md#L18) 已說明延後原因；第 1 步目前的套件清單與實際安裝版本全部吻合，足以保存 smoke test 環境快照。  
   不過，[需求:81](../CLAUDE.md#L81) 仍要求 `uv.lock`；現有清單包含[絕對路徑 editable 安裝:35](../docs/step1-requirements.lock.txt#L35)，也未固定套件來源與雜湊。建立鎖檔時須保留 torch／torchaudio 的 cu128 來源，以及 torchcodec 的 CPU 來源，並驗證乾淨環境重建。

4. **【建議】RTF 算法可信，但應明確標示計時範圍與樣本限制。**  
   [腳本:72](../scripts/step1_smoke.py#L72) 開始計時，經 processor、生成、codec 解碼及 CUDA 同步後結束；RTF 為耗時除以實際音訊長度。它**包含克隆參考音訊處理，排除模型載入與 WAV 寫檔**。JSON、WAV 時長及 RTF 相符，少量末位差異可由四捨五入解釋。  
   [結果:3](../outputs/step1/results.json#L3) 首次中文 RTF 為 **2.178**，第二輪為 **0.681**，支持明顯暖機影響；但六段短音訊不足以推定長旁白的逾時或吞吐量。「首次多約 15 秒」宜標為本次觀察值。

5. **【建議】顯存指標需補充定義與原始紀錄。**  
   [腳本:71](../scripts/step1_smoke.py#L71) 每案重設峰值，因此 `peak_alloc_gib` 可作為包含常駐權重的 PyTorch allocated 峰值；但 [腳本:97](../scripts/step1_smoke.py#L97) 的 `reserved_gib` 是結束時取樣，並非 `max_memory_reserved()`。  
   [報告:32](../docs/step1-report.md#L32) 的載入拆分及 nvidia-smi 程序峰值沒有對應原始日誌。本次 `nvidia-smi` 回傳 `GPU access blocked by the operating system`，無法獨立重驗。建議保留載入、推論與 nvidia-smi 取樣日誌。

6. **【建議】保存 ASR 逐段結果，並另記克隆音色驗收。**  
   [報告:35](../docs/step1-report.md#L35) 宣稱六段 ASR 回轉一致，但專案只有彙總敘述，缺驗證腳本、環境及逐段轉錄。ASR 能檢查文字內容，無法證明音色相似度。  
   克隆呼叫本身符合要求：[腳本:63](../scripts/step1_smoke.py#L63) 確實傳入參考音檔；其 SHA256 亦與官方 `assets/audio/reference_zh.wav` 相同。

7. **【備註】其餘官方範例遵循項目與基本產物檢查通過。**  
   [腳本:15](../scripts/step1_smoke.py#L15) 的四項 SDPA 開關，以及 [腳本:74](../scripts/step1_smoke.py#L74) 的 `max_new_tokens=4096`、取樣參數均與[官方範例:41](../third_party/MOSS-TTS/moss_tts_local_v1.5/README.md#L41)一致；未傳入或更動 `n_vq_for_inference`，並直接保存雙聲道 tensor。  
   實際環境為 uv **0.13.4**、Python **3.12.15**、torch／torchaudio **2.9.1+cu128**、transformers **5.0.0**；MOSS-TTS 為 editable 安裝，未安裝 flash-attn。Git HEAD 與報告的 `934d6826b084c46a0d033402174d5f8ac4ed2519` 一致，checkout 乾淨。  
   `ffprobe` 確認六個 WAV 均為 **pcm_s16le、48 kHz、2 聲道**，時長全部吻合 JSON；六檔亦全部通過 ffmpeg 完整解碼。

8. **【備註】CPU torchcodec 與 snapshot 固定 revision 的偏離合理。**  
   [報告:41](../docs/step1-report.md#L41) 的 CPU torchcodec 替代方案符合目前音訊用途；`readelf` 顯示其連結 `libtorch_cpu.so` 與 FFmpeg 8 函式庫，無直接 NPP 依賴。這不會使 MOSS 神經音訊 tokenizer 改在 CPU 推論。  
   [腳本:42](../scripts/step1_smoke.py#L42) 分別以完整 revision 解析模型與 codec 快照，再傳入本機路徑，確實固定兩者版本；快照檔案完整，檢查的小型 blob 雜湊亦吻合。此作法可避開官方 processor 轉傳 `revision` 的問題。