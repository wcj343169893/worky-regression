#!/bin/bash
# ============================================================
# lib.sh — worky-regression 真機 shell 用例共享庫
# ============================================================
# 移植自 D:\www\app/scripts/lib.sh，差異：
#   - 預設走 USB（DEVICE=adb 序號），connect() 自動判斷 USB / 無線。
#   - 新增 tap_scroll（Compose lazy layout：按鈕未捲入視圖時 bounds=[0,0]，
#     先捲動讓它渲染再點）、set_count（人數欄不吃文字輸入，只能 stepper）、
#     select_job_type（主類型選「餐飲」自動帶子分類，否則後端「工作類型輸入錯誤」）。
#   - bounds 解析改用 python3（比 awk/sed 穩），其餘沿用原 grep 判斷。
#
# 用法：scripts 頂部 `source "$(dirname "$0")/lib.sh"`
# 可用環境變量覆寫：DEVICE / PKG / ADB / UI
# 設計依據（實測）：worky Compose UI 幾乎無 resource-id，唯一可靠信號是 text；
# 定位 = 按 text 找節點→bounds 中心，無 text / 未渲染才退回坐標。
# ============================================================
set -uo pipefail

ADB="${ADB:-adb}"
DEVICE="${DEVICE:-ddc342be}"          # USB 序號；無線則填 ip:5555
PKG_EMP="dev.tw.com.worky.employer.and"
PKG_LAB="dev.tw.com.worky.labor.and"
UI="${UI:-/tmp/worky-ui-$$.xml}"

# ── 連接 / UI 抓取 ──
connect() {
  case "$DEVICE" in
    *:*) "$ADB" connect "$DEVICE" 2>&1 | grep -q "connected\|already" ;;
    *)   "$ADB" -s "$DEVICE" get-state 2>/dev/null | grep -q device ;;
  esac
}

# 自愈 dump：刪舊檔保新鮮；失敗（UiAutomation 被殘留佔用）殺殘留進程重試，最多 5 次。
dump() {
    local i sz
    for i in 1 2 3 4 5; do
        "$ADB" -s "$DEVICE" shell "rm -f /sdcard/ui.xml; uiautomator dump /sdcard/ui.xml" >/dev/null 2>&1
        sz=$("$ADB" -s "$DEVICE" shell "wc -c < /sdcard/ui.xml" 2>/dev/null | tr -d '\r ')
        if [ -n "$sz" ] && [ "$sz" -gt 100 ] 2>/dev/null; then
            "$ADB" -s "$DEVICE" pull "/sdcard/ui.xml" "$UI" >/dev/null 2>&1
            [ -s "$UI" ] && return 0
        fi
        "$ADB" -s "$DEVICE" shell "pkill -9 -f uiautomator" >/dev/null 2>&1
        sleep 1.5
    done
    echo "  ⚠ dump 失敗（裝置掉線或 UiAutomation 被佔用，$UI 已過期）" >&2
    return 1
}

# ── 查詢 ──
has()      { grep -q "text=\"$1\"" "$UI" 2>/dev/null; }
has_like() { grep -q "text=\"[^\"]*$1[^\"]*\"" "$UI" 2>/dev/null; }
texts()    { grep -o 'text="[^"]*"' "$UI" 2>/dev/null | grep -v 'text=""' | sed 's/text="//;s/"$//' | sort -u; }

# 取某 text 元素「有效（非零面積）」中心坐標 -> "cx cy"；零面積/未渲染/找不到 -> 回 1 不輸出。
center_by_text() {
    python3 - "$UI" "$1" <<'PY' 2>/dev/null
import sys, re, xml.etree.ElementTree as ET
ui, label = sys.argv[1], sys.argv[2]
try: root = ET.parse(ui).getroot()
except Exception: sys.exit(1)
for nd in root.iter('node'):
    if nd.get('text','').strip() == label:
        m = re.findall(r'-?\d+', nd.get('bounds',''))
        if len(m)==4 and int(m[2])>int(m[0]) and int(m[3])>int(m[1]):
            print((int(m[0])+int(m[2]))//2, (int(m[1])+int(m[3]))//2); sys.exit(0)
sys.exit(1)
PY
}

# ── 動作 ──
tap_xy()  { "$ADB" -s "$DEVICE" shell "input tap $1 $2" >/dev/null 2>&1; }
swipe()   { "$ADB" -s "$DEVICE" shell "input swipe $1 $2 $3 $4 ${5:-400}" >/dev/null 2>&1; }
back()    { "$ADB" -s "$DEVICE" shell "input keyevent 4" >/dev/null 2>&1; }
type_text() { "$ADB" -s "$DEVICE" shell "input text '$1'" >/dev/null 2>&1; }
dismiss_keyboard() { back; sleep 0.3; }

# 按 text 點中心：dump 須先做好；找到回 0，找不到回 1。
tap_text() {
    local xy; xy=$(center_by_text "$1") || { echo "  ⚠ tap_text 未找到/無有效坐標: $1" >&2; return 1; }
    tap_xy $xy
}
dtap_text() { dump; tap_text "$1"; }

# 點第一個「text 含子串 $1」的有效節點（給工作卡這種長 text：含時段/店名）。回 0/1。
tap_like() {
    local xy; xy=$(python3 - "$UI" "$1" <<'PY' 2>/dev/null
import sys, re, xml.etree.ElementTree as ET
root=ET.parse(sys.argv[1]).getroot(); sub=sys.argv[2]
for nd in root.iter('node'):
    if sub in nd.get('text',''):
        m=re.findall(r'-?\d+', nd.get('bounds',''))
        if len(m)==4 and int(m[2])>int(m[0]) and int(m[3])>int(m[1]):
            print((int(m[0])+int(m[2]))//2, (int(m[1])+int(m[3]))//2); sys.exit(0)
sys.exit(1)
PY
) || { echo "  ⚠ tap_like 未找到含「$1」的節點" >&2; return 1; }
    tap_xy $xy
}

# 捲動直到 text 渲染出真實 bounds 再點（解 Compose lazy layout 的 [0,0] 按鈕，如 下一步/確認發佈）。
# $1=text $2=最多捲幾次(預設4) $3=每次捲動前先下捲(預設1=是)。回 0 成功 / 1 失敗。
tap_scroll() {
    local label="$1" max="${2:-4}" i xy
    for i in $(seq 1 "$max"); do
        dump; xy=$(center_by_text "$label") && { tap_xy $xy; return 0; }
        swipe 540 1500 540 700 400; sleep 1
    done
    echo "  ⚠ tap_scroll 未能讓「$label」渲染" >&2; return 1
}

# ── 時間滾輪（自糾正，中心對齊）──
WHEEL_CENTER_Y="${WHEEL_CENTER_Y:-1862}"
WHEEL_ITEM_H="${WHEEL_ITEM_H:-107}"
_centered_value() {  # $1=後綴(點/分) -> 最接近中心的數字
    python3 - "$UI" "$1" "$WHEEL_CENTER_Y" <<'PY' 2>/dev/null
import sys, re, xml.etree.ElementTree as ET
ui, suf, c = sys.argv[1], sys.argv[2], int(sys.argv[3])
try: root = ET.parse(ui).getroot()
except Exception: sys.exit(0)
best=None; val=None
for nd in root.iter('node'):
    t=nd.get('text','').strip(); mt=re.match(r'^(\d+)'+re.escape(suf)+r'$', t)
    if not mt: continue
    m=re.findall(r'-?\d+', nd.get('bounds',''));
    if len(m)!=4: continue
    cy=(int(m[1])+int(m[3]))//2; d=abs(cy-c)
    if best is None or d<best: best=d; val=mt.group(1)
if val is not None: print(val)
PY
}
_set_wheel() {  # $1=列x $2=目標 $3=後綴
    local colx="$1" target="$2" suffix="$3" cur diff dist i
    target=$((10#$target))
    for i in $(seq 1 12); do
        dump; cur=$(_centered_value "$suffix"); [ -z "$cur" ] && { sleep 0.5; continue; }
        cur=$((10#$cur)); [ "$cur" -eq "$target" ] && return 0
        diff=$((target-cur)); dist=$(( (diff<0?-diff:diff) * WHEEL_ITEM_H ))
        if [ "$diff" -gt 0 ]; then swipe "$colx" 1900 "$colx" $((1900-dist)) 500
        else swipe "$colx" 1900 "$colx" $((1900+dist)) 500; fi
        sleep 0.6
    done
    echo "  ⚠ 滾輪未收斂(目標 $target$suffix)" >&2; return 1
}
# 設定一個時間欄：點未填的「選取時間」→等滾輪渲染→設時/分→確定。$1=時 $2=分(預設0)。
set_time() {
    local h="$1" m="${2:-0}" i
    dump; tap_text "選取時間" || { echo "  ⚠ 無可填的「選取時間」(時間行未在視圖?)" >&2; return 1; }
    for i in $(seq 1 10); do dump; [ -n "$(_centered_value 點)" ] && break; sleep 0.4; done
    [ -z "$(_centered_value 點)" ] && { echo "  ⚠ 時間滾輪未出現" >&2; return 1; }
    _set_wheel "${WHEEL_HOUR_X:-305}" "$h" "點"
    _set_wheel "${WHEEL_MIN_X:-776}"  "$m" "分"
    dump; tap_text "確定" || tap_xy 540 2204; sleep 1
}

# ── 人數 stepper（EditText 不吃文字輸入，只能 [−]/[+]）──
# 讀「人數 / 每日」同行 EditText 現值，與目標比，差幾個就點幾次 [−]/[+]。$1=目標人數。
_count_now() {  # -> 現值
    python3 - "$UI" <<'PY' 2>/dev/null
import sys, re, xml.etree.ElementTree as ET
root=ET.parse(sys.argv[1]).getroot()
# 找 "人數 / 每日" 標籤的 y，再取同行(±60px) 的 EditText 值
lab=None
for nd in root.iter('node'):
    if nd.get('text','').strip()=='人數 / 每日':
        m=re.findall(r'-?\d+', nd.get('bounds','')); lab=(int(m[1])+int(m[3]))//2
if lab is None: sys.exit(0)
for nd in root.iter('node'):
    if 'EditText' in nd.get('class',''):
        m=re.findall(r'-?\d+', nd.get('bounds','')); cy=(int(m[1])+int(m[3]))//2
        if abs(cy-lab)<60 and nd.get('text','').strip().isdigit(): print(nd.get('text').strip()); break
PY
}
_stepper_xy() {  # 回 "minus_x plus_x cy"（人數行的 [−]/[+] 中心）
    python3 - "$UI" <<'PY' 2>/dev/null
import sys, re, xml.etree.ElementTree as ET
root=ET.parse(sys.argv[1]).getroot()
lab=None
for nd in root.iter('node'):
    if nd.get('text','').strip()=='人數 / 每日':
        m=re.findall(r'-?\d+', nd.get('bounds','')); lab=(int(m[1])+int(m[3]))//2
if lab is None: sys.exit(0)
# 同行可點 View：最左=減、最右=加；EditText 在中間
cells=[]
for nd in root.iter('node'):
    m=re.findall(r'-?\d+', nd.get('bounds',''))
    if len(m)!=4: continue
    cy=(int(m[1])+int(m[3]))//2; cx=(int(m[0])+int(m[2]))//2
    if abs(cy-lab)<60 and nd.get('clickable')=='true':
        cells.append((cx,cy))
if len(cells)>=2:
    cells.sort(); print(cells[0][0], cells[-1][0], lab)
PY
}
set_count() {
    local target="$1" cur minus plus cy diff i
    target=$((10#$target))
    dump; cur=$(_count_now); [ -z "$cur" ] && { echo "  ⚠ 找不到人數欄" >&2; return 1; }
    read minus plus cy <<<"$(_stepper_xy)"
    [ -z "${cy:-}" ] && { echo "  ⚠ 找不到人數 stepper" >&2; return 1; }
    cur=$((10#$cur)); diff=$((target-cur))
    if [ "$diff" -gt 0 ]; then for i in $(seq 1 "$diff"); do tap_xy "$plus" "$cy"; sleep 0.2; done
    elif [ "$diff" -lt 0 ]; then for i in $(seq 1 $((-diff))); do tap_xy "$minus" "$cy"; sleep 0.2; done; fi
    dump; [ "$(_count_now)" = "$target" ]
}

# ── 工作類型（主類型選「餐飲」會自動帶子分類；輔導學習無子分類→後端擋）──
# $1=主類型文字(預設 餐飲)。回 0 表單上已顯示該主類型。
select_job_type() {
    local main="${1:-餐飲}"
    dump; tap_text "工作類型選擇" >/dev/null 2>&1 || true
    # 點主類型下拉列（「工作類型選擇」說明下方第一個下拉，y≈534）
    tap_xy 540 534; sleep 2; dump
    tap_text "$main" || { echo "  ⚠ 主類型選單無「$main」" >&2; return 1; }
    sleep 1; dump
    has "$main"   # 表單上應顯示該主類型（子分類由 App 自動帶）
}

# ── 同步：輪詢等待 ──
wait_for() {  # $1=text $2=max秒(預設10)
    local target="$1" max="${2:-10}" e=0
    while [ "$e" -lt "$((max*2))" ]; do dump; has "$target" && return 0; sleep 0.5; e=$((e+1)); done
    return 1
}

# ── 回歸斷言 ──
_PASS=0; _FAIL=0; _FAILED=""
assert_text() { dump; if has "$1";      then _ok "存在: $1"; else _ng "缺少: $1"; fi; }
assert_like() { dump; if has_like "$1";  then _ok "含: $1";   else _ng "缺含: $1"; fi; }
_ok() { _PASS=$((_PASS+1)); echo "  ✅ $1"; }
_ng() { _FAIL=$((_FAIL+1)); _FAILED="${_FAILED}\n    - $1"; echo "  ❌ $1"; }
report() {
    echo ""; echo "════════ 結果：通過 $_PASS / 失敗 $_FAIL ════════"
    [ "$_FAIL" -gt 0 ] && { echo -e "  失敗:$_FAILED"; exit 1; }
    exit 0
}
step() { echo ""; echo "▶ $*"; }
cleanup_ui() { rm -f "$UI" 2>/dev/null; }
trap cleanup_ui EXIT
