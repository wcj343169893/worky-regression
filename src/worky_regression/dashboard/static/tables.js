"use strict";
// 管理表格引擎（打工夥伴 / 商家 / 店鋪，整頁清單，無詳情抽屜）。

import { $, api, apiPost, esc, fmtTs, stars, flag, toast, PAGE, state, OPT } from "./util.js";
import { filterBar, bindFilters, applyFilterParams, fillRows, openModal, closeModal } from "./widgets.js";

// 審核操作欄：待審才顯示「通過 / 駁回」按鈕（資料以接口呼叫後台管理員審核）。
// labor 待審 = valid_status 2（待認證）；shop 待審 = validation_status 1 送審 / 2 審理中。
function reviewCell(key, r) {
  const rv = TABLES[key].review;
  if (!rv || !rv.pending(r)) return `<span class="sub2">—</span>`;
  return `<button class="btn mini ok rv-btn" data-id="${r.id}" data-act="approve">通過</button>` +
    `<button class="btn mini no rv-btn" data-id="${r.id}" data-act="reject">駁回</button>`;
}

export const TABLES = {
  labors: {
    title: "打工夥伴管理", url: "/api/labors", ph: "搜尋 手機 / 帳號 / ID…",
    filters: [{ key: "is_profile_complete", options: OPT.profileComplete }],
    review: { url: "/api/labors/review", noun: "打工夥伴", pending: (r) => Number(r.valid_status) === 2 },
    columns: [
      ["ID", (r) => `<span class="mono">${r.id}</span>`],
      ["手機", (r) => esc(r.phone)],
      ["帳號", (r) => `<span class="sub2">${esc(r.username || "-")}</span>`],
      ["狀態", (r) => `<span class="pill">${r.status}</span>`],
      ["認證", (r) => `<span class="pill">${r.valid_status}</span>`],
      ["評分", (r) => `${stars(r.rating_stars)} <span class="sub2">(${r.evaluation_count})</span>`],
      ["接案/取消/未到", (r) => `${r.job_count} / ${r.canceled_count} / ${r.no_show_count}`, "num"],
      ["扣分", (r) => r.penalty_points, "num"],
      ["最後登入", (r) => `<span class="sub2">${fmtTs(r.last_login_at)}</span>`],
      ["審核", (r) => reviewCell("labors", r)],
    ],
  },
  employers: {
    title: "商家管理", url: "/api/employers", ph: "搜尋 手機 / ID…",
    filters: [{ key: "is_payment_locked", options: OPT.paymentLocked }],
    columns: [
      ["ID", (r) => `<span class="mono">${r.id}</span>`],
      ["手機", (r) => esc(r.phone)],
      ["狀態", (r) => `<span class="pill">${r.status}</span>`],
      ["店鋪數", (r) => `${r.shop_count} / ${r.shop_upper_limit}`, "num"],
      ["付款鎖定", (r) => flag(r.is_payment_locked)],
      ["付款失敗", (r) => r.payment_failed_count, "num"],
      ["最後登入", (r) => `<span class="sub2">${fmtTs(r.last_login_at)}</span>`],
      ["建立", (r) => `<span class="sub2">${fmtTs(r.created_at)}</span>`],
    ],
  },
  shops: {
    title: "店鋪管理", url: "/api/shops", ph: "搜尋 名稱 / ID…",
    filters: [
      { key: "validation_status", options: OPT.shopValidStatus },
      { key: "validation_type", options: OPT.shopValidType },
    ],
    review: { url: "/api/shops/review", noun: "店鋪", pending: (r) => [1, 2].includes(Number(r.validation_status)) },
    columns: [
      ["ID", (r) => `<span class="mono">${r.id}</span>`],
      ["店鋪", (r) => `<div class="strong">${esc(r.name || "-")}</div><div class="sub2">${esc(r.branch_name || "")}</div>`],
      ["商家ID", (r) => `<span class="mono">${r.employer_id}</span>`],
      ["地區", (r) => `${r.city || "-"} ${r.district || ""}`],
      ["驗證", (r) => `<span class="pill">type ${r.validation_type} / st ${r.validation_status}</span>`],
      ["工作數", (r) => `${r.job_count} <span class="sub2">已發 ${r.published_job_count}</span>`, "num"],
      ["評分", (r) => `${stars(r.rating_stars)} <span class="sub2">(${r.evaluation_count})</span>`],
      ["建立", (r) => `<span class="sub2">${fmtTs(r.created_at)}</span>`],
      ["審核", (r) => reviewCell("shops", r)],
    ],
  },
};

export async function renderTable(key) {
  const cfg = TABLES[key];
  const s = state[key] || (state[key] = { q: "", page: 0, filters: {} });
  s.filters = s.filters || {};
  $("view").innerHTML = `
    <div class="view-head">
      <h2>${esc(cfg.title)}</h2>
      <div class="filters">
        <input type="search" id="q" placeholder="${esc(cfg.ph)}" value="${esc(s.q)}" />
        ${filterBar(cfg, s)}
      </div>
    </div>
    <div class="card">
      <div class="table-wrap"><table>
        <thead><tr>${cfg.columns.map((c) => `<th class="${c[2] === "num" ? "num" : ""}">${c[0]}</th>`).join("")}</tr></thead>
        <tbody id="rows"></tbody>
      </table></div>
      <div class="pager">
        <button class="btn ghost" id="first">« 首頁</button>
        <button class="btn ghost" id="prev">‹ 上一頁</button>
        <span id="pginfo"></span>
        <button class="btn ghost" id="next">下一頁 ›</button>
        <button class="btn ghost" id="last">尾頁 »</button>
      </div>
    </div>`;
  let t; $("q").oninput = (e) => { clearTimeout(t); s.q = e.target.value; t = setTimeout(() => { s.page = 0; loadTableList(key); }, 350); };
  bindFilters(cfg, s, () => loadTableList(key));
  // 審核按鈕：事件委派綁在 tbody（loadTableList 只換 innerHTML，tbody 元素本身保留）
  if (cfg.review) $("rows").addEventListener("click", (e) => {
    const btn = e.target.closest(".rv-btn");
    if (btn) doReview(key, Number(btn.dataset.id), btn.dataset.act === "approve");
  });
  loadTableList(key);
}

async function postReview(key, payload) {
  try {
    const r = await apiPost(TABLES[key].review.url, payload);
    toast(r.message || "已送出審核");
    loadTableList(key);   // 重新載入以反映狀態變化
  } catch (e) { toast(e.message); }
}

function doReview(key, id, approve) {
  const rv = TABLES[key].review;
  if (approve) {
    if (confirm(`確定「通過」${rv.noun} #${id} 的審核？`)) postReview(key, { id, approve: true });
    return;
  }
  // 駁回：可填原因（店鋪寫入 other_reason；打工夥伴若留空則用預設理由）
  openModal(`
    <h3>駁回 ${rv.noun} #${id}</h3>
    <p class="set-hint">填寫駁回原因（可留空，後台會記錄通用理由）。</p>
    <textarea id="rv-reason" rows="4" style="width:100%" placeholder="駁回原因…"></textarea>
    <div class="set-actions" style="margin-top:12px">
      <button class="btn no" id="rv-confirm">確認駁回</button>
      <button class="btn ghost" id="rv-cancel">取消</button>
    </div>`);
  $("rv-cancel").onclick = closeModal;
  $("rv-confirm").onclick = () => {
    const reason = $("rv-reason").value.trim();
    closeModal();
    postReview(key, { id, approve: false, other_reason: reason });
  };
}

async function loadTableList(key) {
  const cfg = TABLES[key], s = state[key];
  const params = new URLSearchParams({ q: s.q, limit: PAGE, offset: s.page * PAGE });
  applyFilterParams(params, cfg, s);
  if ($("rows")) $("rows").style.opacity = ".45";
  const data = await api(cfg.url + "?" + params).catch((e) => (toast(e.message), null));
  if (!data || !$("rows")) return;
  fillRows(cfg, data, s, () => loadTableList(key));
}
