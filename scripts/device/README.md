# scripts/device — 真機 job 三段 shell 用例

打工/商家 App 是 Jetpack Compose（多 `clickable=false`）+ 滾輪/日曆動態邏輯，
**shell+adb 比 maestro 適合**（maestro 難做「讀狀態→算→動」的滾輪/翻月/stepper）。
本目錄移植自 `D:\www\app`（同支 Redmi K30i），改走本機 USB（`DEVICE=ddc342be`）並修掉
PUBLISH=1 才會踩到的三個缺口。`device_runner.py`（maestro 軌）負責冒煙/視覺斷言，
這套 shell 用例負責複雜表單流程，兩者並存。

## 跑法

```bash
# 三段串跑（會真發佈一筆今天 19:00-21:00 / 3人 / $250 的工作）
bash scripts/device/run-all.sh

# 單段
bash scripts/device/publish.sh                    # 付款前安全停止（不送出）
PUBLISH=1 bash scripts/device/publish.sh          # 真送出
MATCH="19:00-21:00" bash scripts/device/apply.sh   # 打工端應徵
MATCH="19:00-21:00" bash scripts/device/approve.sh # 商家同意第一位未處理應徵者
```

參數見各腳本頭部（`OFFSET_DAYS/JOB_YMD/START_H/DURATION_H/SALARY/COUNT/JOB_TYPE/PUBLISH`、`MATCH`）。
裝置覆寫：`DEVICE=192.168.101.185:5555`（無線）或 `ADB=/path/to/adb`。

## 三個修正（相對原 D:\www\app harness）

1. **按鈕 lazy layout**：`下一步`/`確認發佈`/`同意上工` 未捲入視圖時 bounds=`[0,0]`，
   tap_text 撲空。改用 `tap_scroll`（捲動直到渲染出真實 bounds 再點）。
2. **人數欄不吃文字輸入**：原 `type_text` 設人數會變錯值（實測 20）。改用 `set_count`（讀現值→
   stepper `[−]/[+]` 點到目標）。
3. **工作類型子分類**：「輔導學習」無子分類→後端發佈擋「工作類型輸入錯誤」。`select_job_type`
   預設選「餐飲」會自動帶子分類（外場服務人員/點餐送餐）才有效。

其他關卡（已處理）：開始時間 >6 分鐘後（過近彈確認）；環境照片可略過；工作內容必填（adb 不能
打中文→填 ASCII）；付款選信用卡（測試卡見 memory `payment-binding-test-card`，*2222 已綁）；
僱傭關係投保提示→確認；付款方式選一次後記住。

## 限制

- 坐標（如 發佈全新的工作 540,1242、翻月 807,995）是 1080×2400 + 當前 App 版本實測值，
  換機型 / App 改版需重標定。
- 同裝置勿與 maestro worker / Maestro Studio 同時跑（會搶 adb / 重裝 driver）。
- `apply.sh`/`approve.sh` 以 `MATCH`（時段子串）定位工作，同時段多筆會點到第一筆。
