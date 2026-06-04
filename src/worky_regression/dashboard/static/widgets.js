"use strict";
// 共用 UI 元件：篩選列、翻頁、清單列渲染、詳情抽屜與小區塊。

import { $, esc, PAGE } from "./util.js";

// ── 條件篩選列（看板與管理表共用）──────────────────────────────────────────
export function filterBar(cfg, s) {
  return (cfg.filters || []).map((flt) => {
    const val = s.filters[flt.key] ?? "";
    if (flt.type === "num")
      return `<input type="number" class="flt" id="flt-${flt.key}" placeholder="${esc(flt.label)}" value="${esc(val)}" />`;
    const opts = flt.options.map(([v, l]) =>
      `<option value="${v}" ${String(val) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("");
    return `<select class="flt" id="flt-${flt.key}">${opts}</select>`;
  }).join("");
}
export function bindFilters(cfg, s, rerender) {
  (cfg.filters || []).forEach((flt) => {
    const el = $("flt-" + flt.key);
    if (!el) return;
    const apply = () => { s.filters[flt.key] = el.value; s.page = 0; rerender(); };
    if (flt.type === "num") { let t; el.oninput = () => { clearTimeout(t); t = setTimeout(apply, 450); }; }
    else el.onchange = apply;
  });
}
export function applyFilterParams(params, cfg, s) {
  (cfg.filters || []).forEach((flt) => {
    const v = s.filters[flt.key];
    if (v != null && v !== "") params.set(flt.key, v);
  });
}

// 翻頁元件共用：依 total 設定 #pginfo 與首/上/下/尾按鈕
export function setupPager(s, total, reload) {
  const totalPages = Math.max(1, Math.ceil(total / PAGE));
  $("pginfo").innerHTML = `第 <b>${s.page + 1}</b> / ${totalPages.toLocaleString()} 頁`;
  const atFirst = s.page === 0, atLast = s.page + 1 >= totalPages;
  $("first").disabled = atFirst; $("prev").disabled = atFirst;
  $("next").disabled = atLast; $("last").disabled = atLast;
  const go = (p) => { const np = Math.min(Math.max(0, p), totalPages - 1); if (np !== s.page) { s.page = np; reload(); } };
  $("first").onclick = () => go(0);
  $("prev").onclick = () => go(s.page - 1);
  $("next").onclick = () => go(s.page + 1);
  $("last").onclick = () => go(totalPages - 1);
}

// 渲染清單列 + 翻頁狀態（看板 / 管理表共用）；只動 #rows 與 pager，不重建外殼。
// reload：翻頁時重抓清單；onView(sn, tr)：點「查看」時開詳情（管理表無詳情可省略）。
export function fillRows(cfg, data, s, reload, onView) {
  const tb = $("rows");
  if (!tb) return;
  const hasAct = !!cfg.detail;
  const colspan = cfg.columns.length + (hasAct ? 1 : 0);
  if (!data.items.length) {
    tb.innerHTML = `<tr class="norow"><td colspan="${colspan}"><div class="empty">沒有符合的資料</div></td></tr>`;
  } else {
    tb.innerHTML = data.items.map((r) => {
      const sn = hasAct ? cfg.snOf(r) : null;
      const cls = hasAct ? (sn === s.selSn ? "sel" : "") : "norow";
      const attr = hasAct ? ` data-sn="${esc(sn)}"` : "";
      const cells = cfg.columns.map((c) => `<td class="${c[2] === "num" ? "num" : ""}">${c[1](r)}</td>`).join("");
      const act = hasAct ? `<td class="act"><button class="btn view-btn" data-sn="${esc(sn)}">查看</button></td>` : "";
      return `<tr${attr} class="${cls}">${cells}${act}</tr>`;
    }).join("");
    if (hasAct && onView) tb.querySelectorAll(".view-btn").forEach((btn) =>
      btn.onclick = () => onView(btn.dataset.sn, btn.closest("tr")));
  }
  tb.style.opacity = "1";
  setupPager(s, data.total, reload);
}

// ── 詳情抽屜 ────────────────────────────────────────────────────────────────
export function openDrawer(html) {
  $("drawer-body").innerHTML = html || "";
  $("drawer").classList.add("open"); $("drawer-mask").classList.add("open");
}
export function closeDrawer() {
  $("drawer").classList.remove("open"); $("drawer-mask").classList.remove("open");
}

// 詳情區塊組件
export function f(k, v) { return `<div class="f"><span class="k">${k}</span><span class="v">${v}</span></div>`; }
export function mini(headers, rows) {
  if (!rows.length) return `<div class="sub2">（無）</div>`;
  return `<table class="minitable"><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}
