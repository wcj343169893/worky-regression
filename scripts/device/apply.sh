#!/bin/bash
# ============================================================
# apply.sh — 真機：打工端應徵指定工作
# ============================================================
# 用法：MATCH="19:00-21:00" bash scripts/device/apply.sh
# 參數：
#   MATCH   工作卡上用來定位的子串（時段或店名，預設 19:00-21:00）。
# 前置：該工作已由 publish.sh 發佈，且打工端帳號可見（首頁「找工作」推薦清單）。
# ============================================================
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
PKG="$PKG_LAB"
MATCH="${MATCH:-19:00-21:00}"

echo "=== 應徵：定位含「$MATCH」的工作 ==="

step "[1] 冷啟動打工端（找工作首頁）"
connect || { echo "FATAL 連不上 $DEVICE"; exit 1; }
"$ADB" -s "$DEVICE" shell "am force-stop $PKG" >/dev/null 2>&1
"$ADB" -s "$DEVICE" shell "monkey -p $PKG -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1
sleep 7; dump

step "[2] 點開工作卡（含 $MATCH）"
# 推薦清單在頂；剛發佈的工作通常在最前。捲一點以防被 banner 擋。
for i in 1 2 3; do dump; has_like "$MATCH" && break; swipe 540 1600 540 1100 400; sleep 0.8; done
has_like "$MATCH" || { echo "  ❌ 首頁找不到含「$MATCH」的工作"; exit 1; }
tap_like "$MATCH"; sleep 3
assert_text "工作內容"               # 工作詳情頁

step "[3] 申請這份工作 → 確認"
tap_scroll "申請這份工作"; sleep 2; dump
wait_for "是否確定要申請此工作" 6 || true
tap_text "確認" || tap_xy 746 1525; sleep 3; dump

step "[4] 校驗"
assert_text "申請成功"
report
