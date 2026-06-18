"use strict";
// 共用工具：DOM/fetch helper、格式化、enum 選項、跨頁籤共享狀態。

export const $ = (id) => document.getElementById(id);

export const api = async (path) => {
  const r = await fetch(path);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
  return j;
};
export const apiPost = async (path, body) => {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}) });
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
  return j;
};

export const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
export const pad = (n) => String(n).padStart(2, "0");

export const resBadge = (st) => st === "passed" ? `<span class="badge b-done">通過</span>`
  : st === "failed" ? `<span class="badge b-failed">失敗</span>`
  : st === "skipped" ? `<span class="badge b-draft">略過</span>`
  : st === "running" ? `<span class="badge b-running">執行中</span>`
  : st === "waiting" ? `<span class="badge b-waiting" title="長延時工作：已掛起，到表定時間由 resume_worker 自動喚醒續跑（點「查看」看為什麼等這麼久）">⏳ 等待中</span>`
  : st === "resuming" ? `<span class="badge b-running">喚醒中</span>`
  : st === "interrupted" ? `<span class="badge b-canceled" title="執行途中看板進程終止，執行期上下文（帳號 token、擷取變數）已遺失，無法續跑；請點「執行」重新跑整支用例">中斷</span>`
  : `<span class="badge b-draft">${esc(st || "-")}</span>`;

// 長延時倒數格式化（至 resume_at）：跨日顯示「N天 HH:MM:SS」，到點顯示「喚醒中…」
export function fmtCountdown(sec) {
  if (sec == null) return "—";
  if (sec <= 0) return "喚醒中…";
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (d ? `${d}天 ` : "") + `${pad(h)}:${pad(m)}:${pad(s)}`;
}

export function fmtTs(v) {
  if (!v) return "-";
  const d = new Date(Number(v) * 1000);
  if (isNaN(d)) return "-";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
// 秒級時間戳格式化（步驟執行時刻用，fmtTs 只到分鐘不夠分辨相鄰步驟）
export function fmtTsS(v) {
  if (!v) return "-";
  const d = new Date(Number(v) * 1000);
  if (isNaN(d)) return "-";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
export function fmtDate8(v) {
  const s = String(v || "");
  return s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : (s || "-");
}
export const money = (n) => (n == null ? "-" : "NT$" + Number(n).toLocaleString());
export const laborName = (o) => (!o ? "-" : esc(o.phone || o.username || ("#" + o.id)));
export const shopName = (o) => (!o ? "-" : esc(o.name || o.branch_name || ("#" + o.id)));
export const empName = (o) => (!o ? "-" : esc(o.phone || ("#" + o.id)));
export const stars = (n) => (n == null ? "-" : `<span class="stars">★ ${Number(n).toFixed(1)}</span>`);
export const flag = (v, on = "是", off = "否") =>
  v ? `<span class="flag-on">${on}</span>` : `<span class="flag-off">${off}</span>`;

export function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(t._t); t._t = setTimeout(() => (t.hidden = true), 2600);
}

export const PAGE = 20;

// ── 分頁狀態 ↔ URL（hash 查詢段：#view/...?page=N&limit=M）──────────────────
// 翻頁/載入時用 history.replaceState 把 page/limit 寫回 URL（不觸發 hashchange、
// 不重渲染、不增加歷史記錄）；渲染時讀回還原——刷新 / 分享連結都停在同一頁。
// URL 的 page 為 1-based（給人看），內部一律 0-based。
export function urlPager(defLimit = PAGE) {
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  const page = Math.max(0, (parseInt(q.get("page"), 10) || 1) - 1);
  const limit = Math.min(200, Math.max(1, parseInt(q.get("limit"), 10) || defLimit));
  return { page, limit };
}
export function syncUrlPager(page, limit) {
  const path = location.hash.replace(/^#/, "").split("?")[0];
  const q = new URLSearchParams(location.hash.split("?")[1] || "");
  q.set("page", String((page || 0) + 1));
  q.set("limit", String(limit));
  history.replaceState(null, "", `#${path}?${q.toString()}`);
}

export const CAT_COLOR = { matching: "#5b8cff", recruited: "#2bd4c0", running: "#ffb454",
  done: "#3ddc97", failed: "#ff6b6b", canceled: "#5d6b8c", draft: "#5d6b8c" };

// 承攬制進度碼 → 色塊分類
export function contractCat(code) {
  return ({ 1: "matching", 2: "matching", 3: "recruited", 4: "recruited",
    5: "running", 6: "running", 7: "done", 8: "failed", 9: "failed", 10: "canceled" }[code]) || "draft";
}
export function badge(cat, label) { return `<span class="badge b-${cat}">${esc(label)}</span>`; }

export const OPT = {
  payStatus: [["", "全部付款狀態"], ["0", "無"], ["1", "等待付款"], ["2", "付款完成"], ["3", "準備結算"], ["4", "已結算"], ["5", "結算失敗"], ["6", "待母單結算"], ["7", "付款中"], ["31", "自動取消"]],
  payMethod: [["", "全部付款方式"], ["1", "FunPoint信用卡"], ["2", "信用卡"], ["3", "ATM"]],
  profileComplete: [["", "個資完成?"], ["1", "已完成"], ["0", "未完成"]],
  paymentLocked: [["", "付款鎖定?"], ["1", "已鎖定"], ["0", "未鎖定"]],
  shopValidStatus: [["", "全部驗證狀態"], ["0", "草稿"], ["1", "已送審"], ["2", "審理中"], ["3", "已通過"], ["4", "未通過"]],
  shopValidType: [["", "全部驗證類型"], ["0", "未填寫"], ["1", "統一編號"], ["2", "身分證號"]],
};

// 各頁籤的檢視狀態（q / page / category / selSn / filters），跨重繪保留
export const state = {};
