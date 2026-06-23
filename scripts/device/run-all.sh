#!/bin/bash
# ============================================================
# run-all.sh — 真機 job 三段串跑：發佈 → 應徵 → 同意
# ============================================================
# 用法：
#   bash scripts/device/run-all.sh                 # 今天 19:00-21:00 / 3人 / $250 全跑（會真發佈）
#   START_H=20 COUNT=2 bash scripts/device/run-all.sh
# 說明：publish 用 PUBLISH=1 真送出，apply/approve 以時段子串定位同一筆工作。
#   單裝置序列化；過程約數分鐘。任一段失敗即中止（exit code 反映）。
# ============================================================
HERE="$(cd "$(dirname "$0")" && pwd)"
START_H="${START_H:-19}"; DURATION_H="${DURATION_H:-2}"; END_H="${END_H:-$((START_H+DURATION_H))}"
MATCH="${MATCH:-${START_H}:00-${END_H}:00}"

echo "######## [1/3] 發佈 ########"
PUBLISH=1 START_H="$START_H" DURATION_H="$DURATION_H" bash "$HERE/publish.sh" || { echo "發佈失敗，中止"; exit 1; }
echo "######## [2/3] 應徵（MATCH=$MATCH）########"
MATCH="$MATCH" bash "$HERE/apply.sh" || { echo "應徵失敗，中止"; exit 1; }
echo "######## [3/3] 同意（MATCH=$MATCH）########"
MATCH="$MATCH" bash "$HERE/approve.sh" || { echo "同意失敗，中止"; exit 1; }
echo "######## 三段全綠 ########"
