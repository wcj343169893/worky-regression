#!/bin/bash
# ============================================================
# approve.sh — 真機：雇主端同意（錄取）應徵者
# ============================================================
# 用法：MATCH="19:00-21:00" bash scripts/device/approve.sh
# 參數：
#   MATCH   媒合列表上用來定位工作卡的子串（時段，預設 19:00-21:00）。
# 前置：該工作已被打工端應徵（工單明細「未處理(>=1)」）。同意第一位未處理應徵者。
# ============================================================
source "$(cd "$(dirname "$0")" && pwd)/lib.sh"
PKG="$PKG_EMP"
MATCH="${MATCH:-19:00-21:00}"

echo "=== 同意：定位含「$MATCH」的工作的第一位應徵者 ==="

step "[1] 冷啟動雇主端（媒合列表）"
connect || { echo "FATAL 連不上 $DEVICE"; exit 1; }
"$ADB" -s "$DEVICE" shell "am force-stop $PKG" >/dev/null 2>&1
"$ADB" -s "$DEVICE" shell "monkey -p $PKG -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1
sleep 6; dump
# 確保在媒合 tab（首頁預設即媒合列表；若不在則點底部「媒合」）
has "媒合列表" || { tap_text "媒合" || tap_xy 405 2330; sleep 2; dump; }

step "[2] 點開工作卡（含 $MATCH）→ 工單明細"
for i in 1 2 3; do dump; has_like "$MATCH" && break; swipe 540 1600 540 1100 400; sleep 0.8; done
has_like "$MATCH" || { echo "  ❌ 媒合列表找不到含「$MATCH」的工作"; exit 1; }
tap_like "$MATCH"; sleep 3; dump
assert_text "工單明細"
assert_like "未處理"

step "[3] 開應徵者「夥伴資訊」"
has "未處理(0)" && { echo "  ❌ 無未處理應徵者（未處理(0)）"; report; }
tap_scroll "夥伴資訊"; sleep 3; dump
assert_text "夥伴資訊"

step "[4] 同意上工 → 確認"
tap_scroll "同意上工"; sleep 2; dump
wait_for "確認同意申請者上工" 6 || true
tap_text "確認" || tap_xy 746 1475; sleep 3; dump

step "[5] 校驗"
assert_text "同意成功"
report
