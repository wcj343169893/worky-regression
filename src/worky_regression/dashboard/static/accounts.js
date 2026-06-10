"use strict";
// 帳號池：以 tab 切換 商家 / 打工夥伴 兩個池（qa_accounts），測試登入、啟用/停用。
// 執行期 runner 從此池按 caps + 最久未用輪換配發；停用(disabled)者不被配發。
// 版面沿用「測試用例」頁（cases-page / cases-list）：tab 列在上，清單撐滿視窗、表身可捲。

import { $, api, apiPost, esc, fmtTs, toast, OPT } from "./util.js";
import { openModal, mini } from "./widgets.js";

// tab 順序：商家在前、打工夥伴在後（與既有列出順序 employer/labor 一致）。
const ROLE_TABS = [["employer", "商家"], ["labor", "打工夥伴"]];
const ROLE_LABEL = { labor: "打工夥伴", employer: "商家" };
const DEFAULT_ROLE = "employer";   // 預設池（對應乾淨的 #accounts）
const optLabel = (opts, v) => (opts.find(([k]) => k === String(v)) || [null, v])[1];

// 註冊時「可選賦予」的進階能力（基本 active/clean 一律自動帶上，不列）。
// audit_role 純 API 達不到（需 provision 種子），故不在註冊選項。
const SELECTABLE_CAPS = {
  labor: [["verified", "已認證(後台核准)"], ["profile_complete", "資料完整(完整送審)"]],
  employer: [["shop_approved", "店鋪已核准(後台)"], ["verified_shop", "店鋪身分證送審(type2)"]],
};
const BASE_CAPS = { labor: ["active", "clean"], employer: ["active"] };

// 兩池欄位不同：labor 顯示姓名/性別（店鋪欄對 labor 恆空無意義）；employer 維持店鋪。
// 姓名來自 API 註冊時讀回的 profile（工作庫 display_name 加密，種子帳號拿不到則顯示 —）。
const HEAD_COLS = {
  labor: ["ID", "手機", "姓名", "性別", "能力 caps", "狀態", "最近使用"],
  employer: ["ID", "手機", "店鋪", "能力 caps", "狀態", "最近使用"],
};
const GENDER_LABEL = { 0: "不分", 1: "男", 2: "女" };   // 工作庫 gender int 對照

// 渲染當前角色的能力勾選框
function capsCheckboxesHtml(role) {
  return (SELECTABLE_CAPS[role] || []).map(([cap, label]) =>
    `<label class="acc-cap"><input type="checkbox" class="acc-cap-cb" value="${cap}" /> ${esc(label)}</label>`
  ).join("");
}
// 收集勾選的進階能力 + 基本能力（恆非空，確保後端 need_approve 判定正確）
function selectedRegisterCaps(role) {
  const adv = [...document.querySelectorAll(".acc-cap-cb:checked")].map((c) => c.value);
  return [...(BASE_CAPS[role] || []), ...adv];
}

let curRole = "employer";   // 當前選中的池（跨重渲染保留）
let groupsByRole = {};      // 最近一次抓到的各角色分組（切 tab 不重抓）
const ACC_PAGE_SIZE = 20;   // 每頁筆數（前端切片分頁；/api/accounts 已回各角色完整清單）
let pageByRole = {};        // role -> 當前頁碼（各池獨立、跨重繪保留）

// 列出該商家(employer)的所有店鋪：打 /api/shops?employer_id= 後以彈窗呈現。
async function showShops(aid) {
  try {
    const d = await api(`/api/shops?employer_id=${aid}&limit=200`);
    const items = d.items || [];
    const rows = items.map((s) => [
      `<span class="mono">#${s.id}</span>`,
      `<div class="strong">${esc(s.name || "-")}</div>`,
      esc(s.branch_name || "—"),
      esc((s.city_name || "") + (s.district_name || "") || "—"),
      `<span class="pill">${esc(optLabel(OPT.shopValidStatus, s.validation_status))}</span>`,
    ]);
    openModal(`<h3>商家 #${aid} 的店鋪（${items.length}）</h3>` +
      mini(["ID", "店名", "分店", "城市", "驗證狀態"], rows));
  } catch (err) { toast(err.message); }
}

function stateBadge(it) {
  if (it.state === "disabled") return `<span class="badge b-failed">已停用</span>`;
  if (it.leased_active) return `<span class="badge b-running">使用中</span>`;
  return `<span class="badge b-done">可用</span>`;
}

function rowHtml(it) {
  const caps = (it.caps || []).map((c) => `<span class="pill">${esc(c)}</span>`).join(" ") || `<span class="sub2">—</span>`;
  // 中段欄位按角色：labor=姓名+性別、employer=店鋪（與 HEAD_COLS 對應）
  const mid = it.role === "labor"
    ? `<td>${it.display_name ? `<span class="acc-name" title="${esc(it.display_name)}">${esc(it.display_name)}</span>` : "—"}</td>
       <td>${it.gender != null ? esc(GENDER_LABEL[it.gender] ?? String(it.gender)) : "—"}</td>`
    : `<td>${it.shop_id != null ? "#" + it.shop_id : "—"}</td>`;
  return `<tr data-aid="${it.account_id}" data-role="${esc(it.role)}">
    <td><span class="mono">#${it.account_id}</span></td>
    <td>${esc(it.phone || "—")}</td>
    ${mid}
    <td>${caps}</td>
    <td>${stateBadge(it)}</td>
    <td><span class="sub2">${it.last_used_at ? fmtTs(it.last_used_at) : "—"}</span></td>
    <td class="act">
      <button class="btn mini test-btn">測試登入</button>
      ${it.role === "employer" ? `<button class="btn mini shops-btn">店鋪</button>` : ""}
      <button class="btn mini ${it.state === "disabled" ? "ok" : "no"} toggle-btn">${it.state === "disabled" ? "啟用" : "停用"}</button>
      <span class="acc-msg sub2"></span>
    </td></tr>`;
}

// 重繪當前角色的清單（標題 / 提醒 / 表身）並重新綁定列上的按鈕事件。
function paintRole() {
  const g = groupsByRole[curRole];
  const label = ROLE_LABEL[curRole] || curRole;
  const title = $("acc-title");
  const warn = $("acc-warn");
  const tbody = $("acc-rows");

  // tab active 態
  $("view").querySelectorAll(".dc-tab[data-role]").forEach((b) =>
    b.classList.toggle("active", b.dataset.role === curRole));

  // 表頭按角色重繪（labor 與 employer 欄位不同；+1 是操作欄）
  const cols = HEAD_COLS[curRole] || HEAD_COLS.employer;
  $("acc-head").innerHTML = cols.map((c) => `<th>${esc(c)}</th>`).join("") + `<th class="act">操作</th>`;

  if (!g || !g.count) {
    title.textContent = `${label}（0 個）`;
    warn.innerHTML = "";
    tbody.innerHTML = `<tr class="norow"><td colspan="${cols.length + 1}"><div class="empty">此池是空的——請先 provision 種子帳號（或由補池 worker 自動補回）</div></td></tr>`;
    updateAccPager(1, 0);
    return;
  }
  title.textContent = `${label}（${g.count} 個，可用 ${g.available}）`;
  // 可換號性：available 數 < 2 時提醒（換號需要池中有同能力替補）
  warn.innerHTML = g.available < 2
    ? `<span class="set-status no">⚠ 可用僅 ${g.available} 個，登入失敗時無從換號（補池 worker 會自動補回）</span>` : "";
  // 前端切片分頁：取當前頁那一段；頁碼越界（如資料變少）夾回最後一頁
  const pages = Math.max(1, Math.ceil(g.items.length / ACC_PAGE_SIZE));
  let page = pageByRole[curRole] || 0;
  if (page > pages - 1) page = pages - 1;
  pageByRole[curRole] = page;
  tbody.innerHTML = g.items.slice(page * ACC_PAGE_SIZE, (page + 1) * ACC_PAGE_SIZE).map(rowHtml).join("");
  updateAccPager(pages, page);

  tbody.querySelectorAll("tr[data-aid]").forEach((tr) => {
    const aid = Number(tr.dataset.aid), role = tr.dataset.role;
    const msg = tr.querySelector(".acc-msg");
    tr.querySelector(".test-btn").onclick = async (e) => {
      const b = e.target; const old = b.textContent; b.disabled = true; b.textContent = "登入中…";
      msg.className = "acc-msg sub2"; msg.textContent = "";
      try {
        const r = await apiPost("/api/accounts/test-login", { account_id: aid, role });
        msg.textContent = r.message;
        msg.className = "acc-msg " + (r.ok ? "ok" : "no");
      } catch (err) { msg.textContent = err.message; msg.className = "acc-msg no"; }
      finally { b.disabled = false; b.textContent = old; }
    };
    const shopsBtn = tr.querySelector(".shops-btn");
    if (shopsBtn) shopsBtn.onclick = () => showShops(aid);
    tr.querySelector(".toggle-btn").onclick = async (e) => {
      const disabling = e.target.textContent === "停用";
      try {
        await apiPost("/api/accounts/state", { account_id: aid, role, state: disabling ? "disabled" : "available" });
        toast(`#${aid} 已${disabling ? "停用" : "啟用"}`);
        renderAccounts(curRole);   // 內部刷新保留當前 tab（不重置回預設池）
      } catch (err) { toast(err.message); }
    };
  });
}

// 更新分頁器資訊與按鈕禁用態（前端切片，與 cases/markups 同款外觀）。
function updateAccPager(pages, page) {
  const total = groupsByRole[curRole] ? groupsByRole[curRole].items.length : 0;
  const info = $("acc-info");
  if (info) info.innerHTML = `第 <b>${page + 1}</b> / ${pages} 頁 · 共 ${total} 筆`;
  const atFirst = page <= 0, atLast = page >= pages - 1;
  const dis = (id, v) => { const b = $(id); if (b) b.disabled = v; };
  dis("acc-first", atFirst); dis("acc-prev", atFirst);
  dis("acc-next", atLast); dis("acc-last", atLast);
}

// 點 tab：寫入雜湊（#accounts/<role>，預設池用乾淨的 #accounts）讓網址同步、可刷新還原。
// 雜湊變更 → hashchange → route → renderAccounts 自動套用；雜湊未變（重複點同 tab）則手動補繪。
function selectRole(role) {
  const newHash = role === DEFAULT_ROLE ? "accounts" : `accounts/${role}`;
  if (location.hash.replace("#", "") === newHash) { curRole = role; paintRole(); }
  else location.hash = newHash;
}

export async function renderAccounts(tabKey) {
  // 由雜湊（#accounts/<role>）定位當前池；缺省 / 不合法 → 預設池
  curRole = (tabKey === "labor" || tabKey === "employer") ? tabKey : DEFAULT_ROLE;
  const d = await api("/api/accounts").catch((e) => (toast(e.message), null));
  if (!d) return;
  groupsByRole = {};
  (d.groups || []).forEach((g) => { groupsByRole[g.role] = g; });
  // 當前角色若無資料（極少數情況），退回第一個有資料的角色
  if (!groupsByRole[curRole]) {
    const first = ROLE_TABS.find(([r]) => groupsByRole[r]);
    if (first) curRole = first[0];
  }

  // tab 列：沿用 cases 頁的 .dc-tabs/.dc-tab 視覺語彙，帶各池筆數
  const tabsHtml = ROLE_TABS.map(([role, label]) => {
    const g = groupsByRole[role];
    const cnt = g ? g.count : 0;
    return `<button class="dc-tab${role === curRole ? " active" : ""}" data-role="${role}">${esc(label)}<span class="dc-soon">${cnt}</span></button>`;
  }).join("");

  $("view").innerHTML = `
    <div class="cases-page">
      <div class="view-head"><h2>帳號池</h2>
        <span class="sub2">執行期按能力(caps) + 最久未用輪換配發；停用者不配發。測試登入會實際打被測登入 API。</span></div>
      <div class="dc-tabs">${tabsHtml}</div>
      <div class="card cases-list">
        <div class="panel-head"><h3 id="acc-title"></h3><span id="acc-warn"></span>
          <span class="acc-reg">
            <button class="btn ghost" id="acc-init-btn" title="全清當前庫池列，按能力分群各建 3 個（含 provision 種子補 audit_role）。耗時較長">⟳ 初始化</button>
            <span class="acc-reg-caps" title="選擇要賦予的能力（未勾＝僅基本 active/clean）；verified/shop_approved 需後台帳密核准">${capsCheckboxesHtml(curRole)}</span>
            <input type="number" id="acc-reg-n" class="flt" min="1" max="20" value="1" title="要註冊的帳號數（最多 20）" />
            <button class="btn primary" id="acc-reg-btn" title="產 09 手機號自動註冊並依所選能力補資料/送審/核准入池">＋ 註冊入池</button>
          </span>
        </div>
        <div class="table-wrap"><table>
          <thead><tr id="acc-head"></tr></thead>
          <tbody id="acc-rows"></tbody>
        </table></div>
        <div class="pager">
          <button class="btn ghost" id="acc-first">« 首頁</button>
          <button class="btn ghost" id="acc-prev">‹ 上一頁</button>
          <span id="acc-info"></span>
          <button class="btn ghost" id="acc-next">下一頁 ›</button>
          <button class="btn ghost" id="acc-last">尾頁 »</button>
        </div>
      </div>
    </div>`;

  $("view").querySelectorAll(".dc-tab[data-role]").forEach((b) =>
    b.onclick = () => selectRole(b.dataset.role));

  // 註冊入池：對當前角色純 API 建 N 個帳號，依所選能力補資料/送審/核准，完成後重抓清單
  $("acc-reg-btn").onclick = async () => {
    const n = Math.max(1, Math.min(20, Number($("acc-reg-n").value) || 1));
    const caps = selectedRegisterCaps(curRole);   // 含基本 active/clean；勾選的進階能力決定步驟
    const btn = $("acc-reg-btn"); const old = btn.textContent;
    btn.disabled = true; btn.textContent = `註冊中…`;
    try {
      const r = await apiPost("/api/accounts/register", { role: curRole, n, caps });
      const rows = r.results || [];
      const fails = rows.filter((x) => !x.ok);
      let msg = `註冊入池：${r.ok}/${r.total} 成功`;
      if (fails.length) msg += `；失敗 ${fails.length}（${esc(fails[0].error || "")}）`;
      if (r.auto_review) {
        const skipped = rows.find((x) => x.ok && String(x.review || "").startsWith("skipped:"));
        const revFail = rows.find((x) => x.ok && String(x.review || "").startsWith("failed:"));
        msg += `；審核通過 ${r.reviewed || 0}`;
        if (skipped) msg += `（跳過：${esc((skipped.review || "").slice(8))}）`;
        else if (revFail) msg += `（有審核失敗：${esc((revFail.review || "").slice(7))}）`;
      }
      toast(msg);
      renderAccounts(curRole);   // 重抓清單，新帳號入列
    } catch (e) { toast("註冊失敗：" + e.message); btn.disabled = false; btn.textContent = old; }
  };

  // 初始化：全清重建（按能力分群各建 3 個 + provision 種子）。耗時較長，需二次確認
  $("acc-init-btn").onclick = async () => {
    if (!confirm("將【全清】當前庫的帳號池追蹤列（不動後端真實帳號），再按能力分群各建 3 個並送審/核准。\n耗時較長，確定執行？")) return;
    const btn = $("acc-init-btn"); const old = btn.textContent;
    btn.disabled = true; btn.textContent = "初始化中…（數十秒～數分鐘）";
    try {
      const r = await apiPost("/api/accounts/init", { per_cap: 3 });
      const g = (r.groups || []).map((x) => `${x.role}[${(x.target_caps || []).join("+")}]:${x.ok}/${x.total}`).join("，");
      toast(`初始化完成：清 ${r.cleared} 列；分群 ${g}`);
      renderAccounts(curRole);
    } catch (e) { toast("初始化失敗：" + e.message); btn.disabled = false; btn.textContent = old; }
  };

  // 翻頁：改 pageByRole[curRole] 後重繪當前頁（前端切片，不重抓）
  const accPages = () => Math.max(1, Math.ceil((groupsByRole[curRole]?.items.length || 0) / ACC_PAGE_SIZE));
  const gotoAccPage = (p) => { pageByRole[curRole] = p; paintRole(); };
  $("acc-first").onclick = () => { if ((pageByRole[curRole] || 0) > 0) gotoAccPage(0); };
  $("acc-prev").onclick = () => { const c = pageByRole[curRole] || 0; if (c > 0) gotoAccPage(c - 1); };
  $("acc-next").onclick = () => { const c = pageByRole[curRole] || 0; if (c < accPages() - 1) gotoAccPage(c + 1); };
  $("acc-last").onclick = () => { const c = pageByRole[curRole] || 0; const p = accPages() - 1; if (c < p) gotoAccPage(p); };

  paintRole();
}
