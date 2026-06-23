#!/bin/bash
# ============================================================
# publish.sh — 真機：雇主端發佈全新工作（含三項修正，PUBLISH=1 可真送出）
# ============================================================
# 用法：
#   bash scripts/device/publish.sh                    # 付款前停（安全）
#   PUBLISH=1 bash scripts/device/publish.sh          # 真正送出（建立真實工作，刷已綁卡）
# 參數（環境變量，皆可選）：
#   OFFSET_DAYS=0  今天+N天（預設0=今天）  JOB_YMD=2026-06-20 絕對日期（支援跨月）
#   START_H=19     開始時（預設19）        DURATION_H=2 時長→結束（預設2）  END_H=21 直接指定結束
#   SALARY=250     時薪（最低230）         COUNT=3 人數                     JOB_TYPE=餐飲 主類型
#   PUBLISH=0      1才真送出
# 注意：開始時間須 >6 分鐘後（過近會彈確認，已處理）；環境照片略過；工作內容填 ASCII（adb 不能打中文）。
# ============================================================
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
PKG="$PKG_EMP"

OFFSET_DAYS="${OFFSET_DAYS:-0}"
TARGET_YMD="${JOB_YMD:-$(date -d "+${OFFSET_DAYS} days" +%Y-%m-%d)}"
TARGET_D=$(date -d "$TARGET_YMD" +%-d)
TARGET_HDR=$(date -d "$TARGET_YMD" +%Y年%m月)
TARGET_FORM=$(date -d "$TARGET_YMD" +%m/%d)
START_H="${START_H:-19}"; DURATION_H="${DURATION_H:-2}"; END_H="${END_H:-$((START_H+DURATION_H))}"
SALARY="${SALARY:-250}"; COUNT="${COUNT:-3}"; JOB_TYPE="${JOB_TYPE:-餐飲}"; PUBLISH="${PUBLISH:-0}"

echo "=== 發佈：${TARGET_YMD} ${START_H}:00~${END_H}:00 / ${JOB_TYPE} / \$${SALARY} / ${COUNT}人 / PUBLISH=${PUBLISH} ==="

step "[1] 冷啟動雇主端"
connect || { echo "FATAL 連不上 $DEVICE"; exit 1; }
"$ADB" -s "$DEVICE" shell "am force-stop $PKG" >/dev/null 2>&1
"$ADB" -s "$DEVICE" shell "monkey -p $PKG -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1
sleep 6; dump
has "今日不再顯示" && { tap_xy 541 1868; sleep 2; }

step "[2] 進入「發佈全新的工作」"
dump; tap_text "發佈工作" || tap_xy 540 1990; sleep 3
tap_xy 540 1242                                   # 發佈全新的工作（Compose 無 text，固定坐標）
sleep 2; dump
has_like "編輯中的工作會清空" && { tap_text "確認" || tap_xy 746 1411; sleep 2; }
wait_for "工作類型選擇" 10; assert_text "工作類型選擇"

step "[3] 工作類型=${JOB_TYPE}（自動帶子分類）+ 帶入名稱"
select_job_type "$JOB_TYPE" || echo "  ⚠ 工作類型選擇可能未生效"
dtap_text "帶入工作類型"; sleep 1                  # 用類型帶入工作名稱

step "[4] 選日期 ${TARGET_YMD}"
dtap_text "選取日期"; sleep 2; assert_text "確定"
for i in $(seq 1 13); do
    dump; cur=$(grep -o 'text="[0-9]*年[0-9]*月"' "$UI" | head -1 | sed 's/text="//;s/"//')
    [ "$cur" = "$TARGET_HDR" ] && break
    echo "  翻月 $cur → $TARGET_HDR"; tap_xy 807 995; sleep 1
done
dump; tap_text "$TARGET_D"; sleep 1
tap_text "確定" || tap_xy 540 2217; sleep 2; assert_like "$TARGET_FORM"

step "[5] 時間 ${START_H}:00 ~ ${END_H}:00（自糾正滾輪）"
swipe 540 1700 540 1000 500; sleep 1; set_time "$START_H" 0
swipe 540 1700 540 1000 500; sleep 1; set_time "$END_H" 0
assert_like "${START_H}:00"

step "[6] 薪資 \$${SALARY}（最低230）"
for i in 1 2 3; do dump; has "/小時" && break; swipe 540 1600 540 700 500; sleep 0.8; done
yrow=$(center_by_text "/小時" | awk '{print $2}')
[ -n "$yrow" ] && { tap_xy 540 "$yrow"; for i in $(seq 1 6); do "$ADB" -s "$DEVICE" shell input keyevent 67 >/dev/null 2>&1; done; type_text "$SALARY"; dismiss_keyboard; sleep 0.5; }
dump; assert_text "$SALARY"

step "[7] 人數=${COUNT}（stepper，非文字輸入）"
set_count "$COUNT" && echo "  ✅ 人數=$COUNT" || echo "  ❌ 人數設定失敗"

step "[8] 下一步（先捲動渲染）+ 時間過近對話框"
tap_scroll "下一步"; sleep 3; dump
has "工作開始時間過近" && { tap_text "確認" || tap_xy 746 1475; sleep 3; dump; }
assert_text "上傳照片"

step "[9] 照片頁 → 略過環境照片"
tap_scroll "下一步"; sleep 2; dump
has "略過" && { tap_text "略過"; sleep 3; dump; }

step "[10] 工作內容（必填，填 ASCII）"
assert_text "填寫工作內容"
# 點「工作內容」標籤下方第一個 EditText
cxy=$(python3 - "$UI" <<'PY'
import sys,re,xml.etree.ElementTree as ET
root=ET.parse(sys.argv[1]).getroot(); lab=None
for nd in root.iter('node'):
    if nd.get('text','').strip()=='工作內容':
        m=re.findall(r'-?\d+',nd.get('bounds','')); lab=(int(m[1])+int(m[3]))//2
for nd in root.iter('node'):
    if 'EditText' in nd.get('class',''):
        m=re.findall(r'-?\d+',nd.get('bounds','')); cy=(int(m[1])+int(m[3]))//2
        if lab and cy>lab: print((int(m[0])+int(m[2]))//2, cy); break
PY
)
[ -n "$cxy" ] && { tap_xy $cxy; type_text "regression device test job"; dismiss_keyboard; sleep 0.5; }
tap_scroll "下一步"; sleep 3

step "[11] 預覽 → 確認發佈"
wait_for "此為預覽畫面" 8
tap_scroll "確認發佈"; sleep 4; dump

step "[12] 費用頁：驗人數 + 選付款（信用卡）"
assert_like "${COUNT}人"
if has "請選擇付款方式"; then
    tap_text "請選擇付款方式" || tap_xy 793 2062
    for i in $(seq 1 8); do dump; has_like "僱傭關係" && break; sleep 0.5; done
    has_like "僱傭關係" && { tap_text "確認" || tap_xy 746 1525; }
    for i in $(seq 1 8); do dump; has "信用卡" && break; sleep 0.5; done
    tap_text "信用卡" || tap_xy 223 1167; sleep 1
    tap_text "確認" || tap_xy 734 1526
    for i in $(seq 1 10); do dump; has "請選擇付款方式" || break; sleep 0.5; done
fi

if [ "$PUBLISH" != "1" ]; then
    step "安全停止（PUBLISH=1 才真送出）"; report
fi

step "[13] 確認發佈（真送出）"
tap_scroll "確認發佈"; sleep 5; dump
has_like "工作類型輸入錯誤" && echo "  ❌ 工作類型子分類無效（換 JOB_TYPE，如 餐飲）"
has_like "上傳檔案不存在" && echo "  ❌ 環境照片 URL 失效"
assert_text "工作完成發佈"
report
