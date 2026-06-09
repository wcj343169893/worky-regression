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
      `<div class="strong">${esc(s.name || "-")}</div>${s.branch_name ? `<div class="sub2">${esc(s.branch_name)}</div>` : ""}`,
      `<span class="pill">${esc(optLabel(OPT.shopValidStatus, s.validation_status))}</span>`,
    ]);
    openModal(`<h3>商家 #${aid} 的店鋪（${items.length}）</h3>` +
      mini(["店名", "驗證狀態"], rows));
  } catch (err) { toast(err.message); }
}

function stateBadge(it) {
  if (it.state === "disabled") return `<span class="badge b-failed">已停用</span>`;
  if (it.leased_active) return `<span class="badge b-running">使用中</span>`;
  return `<span class="badge b-done">可用</span>`;
}

function rowHtml(it) {
  const caps = (it.caps || []).map((c) => `<span class="pill">${esc(c)}</span>`).join(" ") || `<span class="sub2">—</span>`;
  return `<tr data-aid="${it.account_id}" data-role="${esc(it.role)}">
    <td><span class="mono">#${it.account_id}</span></td>
    <td>${esc(it.phone || "—")}</td>
    <td>${it.shop_id != null ? "#" + it.shop_id : "—"}</td>
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

  if (!g || !g.count) {
    title.textContent = `${label}（0 個）`;
    warn.innerHTML = "";
    tbody.innerHTML = `<tr class="norow"><td colspan="7"><div class="empty">此池是空的——請先 provision 種子帳號（或由補池 worker 自動補回）</div></td></tr>`;
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
            <input type="number" id="acc-reg-n" class="flt" min="1" max="20" value="1" title="要註冊的帳號數（最多 20）" />
            <button class="btn primary" id="acc-reg-btn" title="產 09 手機號自動註冊並補資料入池（只標基本 caps）">＋ 註冊入池</button>
          </span>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>ID</th><th>手機</th><th>店鋪</th><th>能力 caps</th><th>狀態</th><th>最近使用</th><th class="act">操作</th></tr></thead>
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

  // 註冊入池：對當前角色純 API 建 N 個帳號（產 09 手機號→註冊→補資料），完成後重抓清單
  $("acc-reg-btn").onclick = async () => {
    const n = Math.max(1, Math.min(20, Number($("acc-reg-n").value) || 1));
    const btn = $("acc-reg-btn"); const old = btn.textContent;
    btn.disabled = true; btn.textContent = `註冊中… (0/${n})`;
    try {
      const r = await apiPost("/api/accounts/register", { role: curRole, n });
      const fails = (r.results || []).filter((x) => !x.ok);
      toast(`註冊入池：${r.ok}/${r.total} 成功${fails.length ? `；失敗 ${fails.length}（${esc(fails[0].error || "")}）` : ""}`);
      renderAccounts(curRole);   // 重抓清單，新帳號入列
    } catch (e) { toast("註冊失敗：" + e.message); btn.disabled = false; btn.textContent = old; }
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
