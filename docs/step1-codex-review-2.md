**結論：可以進入第 2 步。** 上一輪 codec 精度的阻擋已排除；報告主要數字與原始紀錄一致，bf16 作為第 2 步預設合理。剩餘問題均屬建議。

1. **【備註】codec 精度修正到位，bf16 預設有依據。**  
   [`--codec-dtype` 已設為必填，明確傳入 processor 並保存 summary](../scripts/step1_smoke.py#L33)。[bf16 日誌](../outputs/step1/bf16/smoke.log#L2) 與 [fp32 日誌](../outputs/step1/fp32/smoke.log#L2) 證實兩組 encoder／decoder 權重精度正確，quantizer 皆為 FP32、compute 皆為 BF16。bf16 的 reserved 峰值少 **6.43 GiB**，本次暖機後速度接近，支持採為預設。

2. **【備註】systemd 與 uv.lock 安排到位。**  
   [systemd 紀錄](../docs/step0-systemd.txt#L2) 包含 system／user manager 皆 `running`、兩者 failed unit 清單皆空，以及 PID 1 為 systemd。[報告已將 `uv.lock` 列為第 2 步首項](../docs/step1-report.md#L19)，包含套件來源與乾淨環境重建驗證，符合上一輪要求。

3. **【建議】RTF 算法與表格正確，但速度結論應收斂。**  
   [腳本計時](../scripts/step1_smoke.py#L85) 確實涵蓋 processor、generate、codec decode、CUDA 同步，排除模型載入與 WAV 寫檔；docstring 與報告亦已說明。兩份 `results.json` 與各自 `smoke.log` 完全一致，[報告六列長度／RTF](../docs/step1-report.md#L50) 均吻合四捨五入。  
   不過，[「差異在取樣隨機性範圍內」](../docs/step1-report.md#L59)及[「RTF 相同」](../docs/step1-report.md#L71)缺乏足夠重複樣本支持，宜改為「本次暖機後 RTF 接近」。長段與吞吐限制已正確揭露。

4. **【建議】顯存數字吻合，但指標名稱與歸因需修正。**  
   [`max_memory_reserved()` 已正確使用](../scripts/step1_smoke.py#L111)，原始紀錄齊全：

   | 指標 | bf16 | fp32 |
   |---|---:|---:|
   | codec 載入 allocated | 4.02 GiB | 8.04 GiB |
   | 模型載入後 allocated | 11.77 GiB | 15.79 GiB |
   | allocated 峰值 | 12.04 GiB | 18.05 GiB |
   | reserved 峰值 | 12.28 GiB | 18.71 GiB |
   | nvidia-smi 取樣最大值 | 12908 MiB | 19490 MiB |

   證據：[bf16 smoke](../outputs/step1/bf16/smoke.log#L3)、[fp32 smoke](../outputs/step1/fp32/smoke.log#L3)、[bf16 CSV](../outputs/step1/bf16/nvidia-smi.csv#L113)、[fp32 CSV](../outputs/step1/fp32/nvidia-smi.csv#L62)。

   [runner 使用 `--query-gpu=memory.used`](../scripts/step1_run.sh#L10)，因此報告的「程序峰值」應改為「整卡每 500 ms 取樣最大值」。此外，[增加約 2.3／0.27 GiB](../docs/step1-report.md#L46)是整段推論峰值減載入後用量，尚不能全部歸因於 codec 解碼；[剩餘約 4.5 GB](../docs/step1-report.md#L71)亦缺實際容量紀錄支持。

5. **【建議】ASR 產物已補齊，仍需完善措辭與重現方式。**  
   腳本與兩組各六筆 JSONL 均存在；12 個 WAV 的時長、格式及完整解碼也通過核對。忽略標點／空白後，11 筆轉錄與原文一致；[fp32/en](../outputs/step1/fp32/asr.jsonl#L3)有 `this → the` 替換。[報告雖已揭露](../docs/step1-report.md#L65)，「皆與原文一致」仍宜調整，且無法僅憑轉錄判定差異來自 TTS 或 ASR。音色需人耳確認的限制已記載。  
   另請補實際 faster-whisper 版本與 JSONL 重導向命令；[腳本用法仍保留 `<ver>`](../scripts/asr_check.py#L3)，目前 runner 也未包含 ASR 步驟。

6. **【建議】runner 應傳回 smoke 的失敗狀態。**  
   [`step1_run.sh` 保存了 `RC`，但只印出](../scripts/step1_run.sh#L15)，最後的 `echo` 可能使失敗執行仍回傳成功。建議加入 `exit "$RC"`，並用 `trap` 清理背景取樣程序，避免後續自動驗證誤判。