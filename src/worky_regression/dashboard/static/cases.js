"use strict";
// 測試用例（工作 / 任務）：列用例 + 執行 + AI 用例分解。

import { $, api, apiPost, esc, fmtTs, fmtTsS, fmtCountdown, resBadge, toast, PAGE, state, urlPager } from "./util.js";
import { setupPager, openDrawer, openModal, closeModal } from "./widgets.js";

export const CASES = {
  "cases": { title: "測試用例",
    ph: "例：商家發工作，夥伴申請後商家取消錄取" },
};

// AI 分解領域 tab（可擴充）：
//   key      —— tab 識別碼（all 代表「全部」，不帶 system 篩選）
//   label    —— 顯示名稱
//   system   —— 對應的目標 system（all 為 ""，即全部 / 不指定）
//   ph       —— 切到此 tab 時 textarea 的 placeholder（領域提示語）
//   planned  —— true 表示 planner 尚未支援（標「規劃中」，分解失敗時友善降級）
const DECOMPOSE_TABS = [
  { key: "all", label: "全部", system: "",
    ph: "例：商家發工作，夥伴申請後商家取消錄取" },
  { key: "job", label: "工作", system: "job",
    ph: "工作流程，例：商家發工作，夥伴申請後商家錄取再打卡" },
  { key: "contract", label: "任務", system: "contract",
    ph: "承攬任務流程，例：發案方發布任務，夥伴接案後完成驗收" },
  // 真機軌（B）：清單篩到 system=app 的 Maestro 用例，「執行」直接驅動真機（DeviceRunner）。
  // planned=true 暫時關閉 AI 分解（真機 flow 由 Maestro MCP 互動式編寫，AI 自動產出待後續）。
  { key: "app", label: "📱 真機", system: "app", planned: true,
    ph: "真機 App 流程（Maestro）；AI 自動產生真機 flow 規劃中，請先手寫用例" },
  { key: "labor", label: "打工夥伴", system: "labor", planned: true,
    ph: "打工夥伴帳號生命週期（註冊 / 審核…）— 規劃中，暫不支援分解" },
];

// 使用者自訂 tab（透過「＋新增」由 AI 產生）持久化在 localStorage，跨重整保留。
const CUSTOM_TABS_LS = "wky_decompose_tabs";
function loadCustomTabs() {
  try { const v = JSON.parse(localStorage.getItem(CUSTOM_TABS_LS) || "[]"); return Array.isArray(v) ? v : []; }
  catch { return []; }
}
function saveCustomTabs(tabs) {
  try { localStorage.setItem(CUSTOM_TABS_LS, JSON.stringify(tabs)); } catch { /* 容量/隱私模式忽略 */ }
}
// 把 AI 回傳的 tab 設定落地成一個自訂 tab（產生唯一 key、補預設、判 planned）
function addCustomTab(t) {
  const tabs = loadCustomTabs();
  const tab = {
    key: "custom-" + Date.now(),
    label: t.label || "自訂",
    system: t.system || "",
    ph: t.placeholder || `描述「${t.label || "自訂"}」相關的測試用例…`,
    query: t.query || "",
    // labor/employer 領域 planner 尚不支援分解 → 標規劃中（與內建一致），仍可作清單篩選
    planned: t.system === "labor" || t.system === "employer",
    custom: true,
  };
  tabs.push(tab); saveCustomTabs(tabs);
  return tab;
}
function removeCustomTab(keyName) {
  saveCustomTabs(loadCustomTabs().filter((t) => t.key !== keyName));
}

// 內建 + 自訂 tab 合集；tabByKey 兩邊都找得到
const allTabs = () => DECOMPOSE_TABS.concat(loadCustomTabs());
const tabByKey = (key) => allTabs().find((t) => t.key === key) || DECOMPOSE_TABS[0];

const stepMark = { passed: "✓", failed: "✗", skipped: "·" };

function runResultHtml(res) {
  if (!res || !res.steps) return `<div class="sub2">（無步驟）</div>`;
  return `<div class="run-log">${res.steps.map((st) => {
    const det = st.error ? `<div class="err">${esc(st.error)}</div>`
      : (st.observations && st.observations.saved
        ? `<div class="sub2" style="grid-column:2/-1">saved: ${esc(JSON.stringify(st.observations.saved))}</div>` : "");
    return `<div class="run-step ${st.status}">
      <span class="rs-mark">${stepMark[st.status] || "?"}</span>
      <span class="rs-name">[${st.index}] ${esc(st.name)}</span>
      <span class="rs-ms">${st.elapsed_ms}ms</span>${det}</div>`;
  }).join("")}</div>`;
}

function caseDetailHtml(d) {
  const stepList = d.steps.map((st) => st.kind === "db_exec"
    ? `<div class="cstep"><span class="ci">${st.index}</span><span class="ck-db">db_exec</span><code>${esc(st.sql)}</code></div>`
    : `<div class="cstep"><span class="ci">${st.index}</span><span class="badge b-running">${esc(st.name)}</span></div>`
  ).join("");
  const last = d.last_result;
  return `
    <div class="dhead"><span class="sn">${esc(d.file)} · ${esc(d.system)}</span>
      <h3>${esc(d.id)} <span class="pill">${d.source === "generated" ? "AI 產生" : "內建"}</span>${d.skip ? ` <span class="pill">略過</span>` : ""}</h3>
      <p class="sub2">${esc(d.description || "")}</p></div>
    ${d.skip ? `<div class="skip-banner">⏸ 此用例已標記 <b>略過</b>，執行時不打被測 API：${esc(d.skip_reason || "（未填原因）")}</div>` : ""}
    ${waitExplainHtml(last)}
    <div class="sec"><h4>任務流（${d.steps.length} 步）</h4>${stepList}</div>
    ${actorsSecHtml(last)}
    <div class="sec"><h4>最近執行結果</h4>
      ${last ? `<div class="sub2" style="margin-bottom:8px">${resBadge(last.status)} ${fmtTs(last.started_at)}${last.run_id ? ` · <code>${esc(last.run_id)}</code>` : ""}</div>${runResultHtml(last)}`
        : `<div class="sub2">（尚無執行記錄）</div>`}</div>
    <div class="sec"><h4>YAML</h4><pre class="yaml">${esc(d.yaml)}</pre></div>`;
}

// 長延時掛起說明：點開詳情時解釋「為什麼等這麼久、到底卡在哪一步」。
// 這不是當機——工作排在很久之後（如「明天 13:00」開工），跑完現在段後掛起、釋放帳號，
// 由 resume_worker 到表定時間自動喚醒、在原 job 上重租同批帳號續跑。
function waitExplainHtml(last) {
  if (!last || last.status !== "waiting" || !last.wait) return "";
  const w = last.wait;
  const li = (label, val) => val == null || val === "" ? "" : `<li>${label}：${val}</li>`;
  const stepIdx = w.resume_step_index != null ? `（第 ${w.resume_step_index} 步）` : "";
  return `<div class="sec wait-explain">
    <h4>⏳ 為什麼等這麼久</h4>
    <p class="sub2">此用例是<b>長延時工作</b>：發佈了排在很久之後才開工的工作（如「明天」開工）。
      已跑完「現在」段（發佈 / 申請 / 錄取 / 上班卡）後<b>掛起</b>，不佔用進程死等——
      由 <code>resume_worker</code> 到表定時間自動喚醒、在<b>原 job</b> 上重租同批帳號續跑打卡 / 評價。</p>
    <ul class="wait-facts">
      ${li("正在等", `<b>${esc(w.step_label || "長延時時間點")}</b> ${stepIdx}`)}
      ${li("表定開工", w.job_start_at ? fmtTsS(w.job_start_at) : "")}
      ${li("表定結束", w.job_end_at ? fmtTsS(w.job_end_at) : "")}
      ${li("預計喚醒", `${fmtTsS(w.resume_at)} · 倒數 <span class="wc-val" data-resume-at="${w.resume_at}">…</span>`)}
      ${li("原 job", w.job_sn ? `<code>${esc(String(w.job_sn))}</code>（已綁定，喚醒時重用）` : "")}
    </ul>
    <p class="sub2">無需手動操作；想立刻重測可改用「執行」發一個新 job。</p>
  </div>`;
}

// 參與帳號區塊：列出最近一次執行用到的 actor（角色 / 手機 / id / 型別）。
// 帳號由池配發、每次可能不同，故取自 last_result.actors（隨 run 落地）；無記錄則不顯示此區塊。
const userTypeLabel = (t) => t === 1 ? "商家" : t === 2 ? "打工夥伴" : "";
function actorsSecHtml(last) {
  const actors = (last && last.actors) || {};
  const roles = Object.keys(actors);
  if (!roles.length) return "";
  const rows = roles.map((role) => {
    const a = actors[role] || {};
    return `<tr><td><code>${esc(role)}</code></td><td>${esc(a.phone || "-")}</td>
      <td>${a.user_id != null ? esc(String(a.user_id)) : "-"}</td>
      <td>${esc(userTypeLabel(a.user_type))}</td></tr>`;
  }).join("");
  return `<div class="sec"><h4>參與帳號（最近一次執行）</h4>
    <table class="actors-table"><thead><tr><th>角色</th><th>手機</th><th>ID</th><th>型別</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

export async function renderCases(key, tabKey, drillPath = []) {
  // system 為頁內狀態（"" = 全部），由 AI 分解 tab 控制；tab 記住當前選中的領域 key
  // stack/parentId：主任務→子任務下鑽用的麵包屑堆疊與當前層父 id（頂層為 null）
  const s = state[key] || (state[key] = { q: "", page: 0, system: "", tab: "all", stack: [], parentId: null });
  if (s.tab == null) s.tab = "all";
  if (s.stack == null) s.stack = [];
  if (s.parentId === undefined) s.parentId = null;
  // 由雜湊（#cases/<tabKey>）定位領域 tab：缺省視為「全部」。與當前不同才套用，
  // 避免重新整理按鈕重渲染時誤清掉使用者正在「全部」分頁打的搜尋字。
  const want = tabKey || "all";
  if (want !== s.tab) applyTab(s, want);
  // 以 URL 的下鑽鏈為準還原層級（空 = 頂層）：刷新 / 瀏覽器前進後退都能正確還原。
  // 只在父層真的改變時把分頁歸零，避免同層重渲染誤跳回第一頁。
  const newParent = drillPath.length ? drillPath[drillPath.length - 1] : null;
  if (s.parentId !== newParent) s.page = 0;
  s.stack = drillPath.map((id) => ({ id, label: id }));
  s.parentId = newParent;
  // URL 帶 ?page=N&limit=M（翻頁時寫入）→ 還原分頁狀態（刷新 / 分享連結停在同一頁）；
  // 放在 tab / 下鑽歸零之後，確保「同頁刷新」時 URL 的 page 不被歸零蓋掉。
  if (location.hash.includes("?")) { const up = urlPager(); s.page = up.page; s.limit = up.limit; }
  const cur = tabByKey(s.tab);
  // 內建 + 自訂 tab 依序渲染；自訂 tab 帶可移除的 ✕；末尾再接「＋新增」按鈕
  const tabsHtml = allTabs().map((t) =>
    `<button class="dc-tab${t.key === cur.key ? " active" : ""}" data-tab="${esc(t.key)}">${esc(t.label)}` +
    `${t.custom ? `<span class="dc-del" data-del="${esc(t.key)}" title="移除此領域">✕</span>` : ""}</button>`
  ).join("") +
    `<button class="dc-tab dc-add" id="dc-add" title="用一句話描述，AI 自動建立領域 tab">＋ 新增</button>`;
  $("view").innerHTML = `
    <div class="cases-page">
      <div class="dc-tabs">${tabsHtml}</div>
      <div class="card cases-list">
        <div class="crumbs" id="crumbs"></div>
        <div class="panel-head"><h3>用例清單</h3>
          <button class="btn primary" id="uc-open" title="自然語言用例 → DeepSeek 分解成任務流（存入 generated/）">✨ AI 分解用例</button>
          <button class="btn primary" id="batch-run" disabled title="勾選下方用例後串行逐條執行">▶ 批量執行</button>
          <button class="btn ghost danger" id="clear-all" title="清空所有測試用例與執行紀錄，重新測試（不影響帳號池 / 設定 / 標記）">🗑 清空全部</button>
          <input type="search" id="q" placeholder="搜尋 名稱 / 描述…" value="${esc(s.q)}" /></div>
        <div class="table-wrap"><table>
          <thead><tr><th class="ck-col"><input type="checkbox" id="ck-all" title="全選 / 取消全選" /></th><th class="desc-col">用例 ID / 描述</th><th class="src-col">來源</th><th class="date-col">建立時間</th><th class="num">步驟</th><th class="flow-col">任務流</th><th class="lr-col">最近結果</th><th class="act">操作</th></tr></thead>
          <tbody id="rows"></tbody>
        </table></div>
        <div class="pager">
          <button class="btn ghost" id="first">« 首頁</button>
          <button class="btn ghost" id="prev">‹ 上一頁</button>
          <span id="pginfo"></span>
          <button class="btn ghost" id="next">下一頁 ›</button>
          <button class="btn ghost" id="last">尾頁 »</button>
        </div>
      </div>
    </div>`;
  // 搜尋：回到頂層（清空下鑽堆疊），讓搜尋語意一致。
  // 在子層搜尋 → 走 hash 回頂層（同步 URL）；已在頂層 → 原地刷新（不重建、不失焦，常見情境）。
  let t; $("q").oninput = (e) => {
    clearTimeout(t); s.q = e.target.value;
    t = setTimeout(() => {
      if (s.stack && s.stack.length) goDrill(key, []);
      else loadCases(key);
    }, 300);
  };
  // AI 用例分解改為彈窗（讓清單佔滿頁面高度）：點按鈕才開分解輸入框
  const ucOpen = $("uc-open");
  if (ucOpen) ucOpen.onclick = () => openDecomposeModal(key);
  // 全選勾選框 + 批量執行（主用例與子任務層皆有；勾選列串行逐條跑，共用帳號池避免並行互擾）
  const ckAll = $("ck-all");
  if (ckAll) ckAll.onchange = () => {
    document.querySelectorAll(".row-ck").forEach((c) => { c.checked = ckAll.checked; });
    updateBatchState();
  };
  const batchBtn = $("batch-run");
  if (batchBtn) batchBtn.onclick = () => batchRun(key);
  // 清空全部：二次確認後清掉所有用例與執行紀錄（重新測試）；不動帳號池 / 設定 / 標記。
  const clearBtn = $("clear-all");
  if (clearBtn) clearBtn.onclick = () => confirmClearAll(key);
  // 切 tab（內建或自訂）：整頁重渲染，套用該 tab 的 system / 查詢內容 / placeholder
  $("view").querySelectorAll(".dc-tab[data-tab]").forEach((b) =>
    b.onclick = () => selectTab(key, b.dataset.tab));
  // 「＋新增」：彈出描述輸入框，AI 產生新領域 tab
  const addBtn = $("dc-add");
  if (addBtn) addBtn.onclick = () => openAddTabModal(key);
  // 移除自訂 tab（✕）：停止冒泡避免觸發切 tab；移除的若是當前 tab 則回「全部」
  $("view").querySelectorAll(".dc-del").forEach((x) => x.onclick = (e) => {
    e.stopPropagation();
    const dk = x.dataset.del;
    removeCustomTab(dk);
    // 移除的若是當前 tab → 回「全部」（並同步雜湊）；否則僅重渲染 tab 列
    if (s.tab === dk) selectTab(key, "all");
    else renderCases(key, s.tab);
  });
  loadCases(key);
}

// 套用某 tab 的篩選到頁內狀態（不負責渲染）。
// 篩選優先以 system 為主（結構性、可靠）；只有「無 system」的 tab 才退而用 query 文字搜尋。
// 否則 system + query 兩個條件 AND 起來，常因 query 文字未出現在用例描述而把清單濾成空的。
function applyTab(s, tabKey) {
  const t2 = tabByKey(tabKey);
  s.tab = t2.key;
  s.system = t2.system;          // all / 未判定 → ""
  // 有 system → 只用 system 篩（清空搜尋字，避免 query 再把該領域用例濾光）；
  // 無 system → query 是唯一可用的篩選依據，套用為搜尋字。
  s.q = t2.system ? "" : (t2.query || "");
  resetToRoot(s);                // 回頂層 + 首頁
}

// 點 tab：寫入雜湊（#cases/<tabKey>，全部用乾淨的 #cases）讓網址同步、可刷新還原。
// 雜湊變更 → hashchange → route → renderCases 自動套用；雜湊未變（重複點同 tab）則手動補渲染。
function selectTab(key, tabKey) {
  const t2 = tabByKey(tabKey);
  const newHash = t2.key === "all" ? key : `${key}/${t2.key}`;
  if (location.hash.replace("#", "") === newHash) {
    applyTab(state[key], t2.key);
    renderCases(key, t2.key);
  } else {
    location.hash = newHash;
  }
}

// 「＋新增」彈窗：輸入描述 → POST /api/cases/tab → AI 產生 tab → 落地並切過去
function openAddTabModal(key) {
  openModal(`<div class="add-tab">
    <h3>新增分解領域</h3>
    <p class="sub2">用一句話描述要測試的功能領域，AI 會自動建立對應 tab 與查詢內容。</p>
    <textarea id="nt-desc" rows="3" placeholder="例：打工夥伴註冊與實名審核流程"></textarea>
    <div class="add-tab-actions">
      <button class="btn ghost" id="nt-cancel">取消</button>
      <button class="btn primary" id="nt-ok">確定</button>
    </div>
  </div>`);
  const ok = $("nt-ok");
  $("nt-cancel").onclick = closeModal;
  ok.onclick = async () => {
    const desc = $("nt-desc").value.trim();
    if (!desc) { toast("請先輸入描述"); return; }
    const old = ok.textContent; ok.disabled = true; ok.textContent = "建立中…";
    try {
      const t = await apiPost("/api/cases/tab", { description: desc });
      const tab = addCustomTab(t);   // 產生 key、存 localStorage
      closeModal();
      selectTab(key, tab.key);       // 切到新 tab 並重渲染（自動套用查詢內容）
      toast(`已新增領域「${tab.label}」`);
    } catch (e) {
      toast("新增失敗：" + e.message);
      ok.disabled = false; ok.textContent = old;
    }
  };
  const ta = $("nt-desc"); if (ta) ta.focus();
}

// 「清空全部」確認彈窗：二次確認 → POST /api/cases/clear → 重渲染清單。
// 只清執行類數據（用例 / 執行 / 步驟），不動帳號池、後台設定、頁面標記。
function confirmClearAll(key) {
  openModal(`<div class="add-tab">
    <h3>🗑 清空所有測試用例數據</h3>
    <p class="sub2">將清空<b>所有用例與執行紀錄</b>（含每步結果），看板序號歸零、重新測試。<br>
      此操作<b>不可復原</b>，但<b>不影響</b>帳號池、後台設定與頁面標記；用例定義仍在 YAML，下次載入會自動重新註冊。</p>
    <div class="add-tab-actions">
      <button class="btn ghost" id="ca-cancel">取消</button>
      <button class="btn danger" id="ca-ok">確定清空</button>
    </div>
  </div>`);
  $("ca-cancel").onclick = closeModal;
  const ok = $("ca-ok");
  ok.onclick = async () => {
    const old = ok.textContent; ok.disabled = true; ok.textContent = "清空中…";
    try {
      const r = await apiPost("/api/cases/clear", {});
      closeModal();
      toast(`已清空 ${r.total} 筆數據，可重新測試`);
      renderCases(key, state[key] ? state[key].tab : "all");
    } catch (e) {
      toast("清空失敗：" + e.message);
      ok.disabled = false; ok.textContent = old;
    }
  };
}

// ── 主任務/子任務下鑽：堆疊 + 麵包屑 ────────────────────────────────────────
// 回到頂層：清空下鑽堆疊與當前父 id，並回到第一頁
function resetToRoot(s) { s.stack = []; s.parentId = null; s.page = 0; }

// 下鑽導航的單一入口：把「父用例鏈」寫進 hash（#cases/<tab>/<父id>…），
// 交給 hashchange → route → renderCases 還原層級並 loadCases。這樣下鑽層級就進了 URL，
// 刷新 / 瀏覽器前進後退都能還原。hash 未變（少見）才手動補渲染。
function goDrill(key, drillIds) {
  const s = state[key];
  const tab = s.tab || "all";
  const parts = [key];
  // all 且無下鑽 → 保持乾淨的 #cases；否則一律帶上 tab 段，下鑽段接其後
  if (tab !== "all" || drillIds.length) parts.push(tab);
  parts.push(...drillIds);
  const newHash = parts.join("/");
  if (location.hash.replace("#", "") === newHash) renderCases(key, tab, drillIds);
  else location.hash = newHash;
}

// 下鑽進某用例的子清單：在當前父鏈尾端加上該用例 id
function drillInto(key, c) {
  const s = state[key];
  goDrill(key, (s.stack || []).map((n) => n.id).concat(c.id));
}

// 麵包屑點某層：pop 到該層（idx = -1 代表回頂層「測試用例」）
function popTo(key, idx) {
  const s = state[key];
  goDrill(key, (s.stack || []).slice(0, idx + 1).map((n) => n.id));
}

// 渲染麵包屑：頂層只顯示「測試用例」（不可點）；下鑽後逐層可點返回
function renderCrumbs(key) {
  const el = $("crumbs");
  if (!el) return;
  const s = state[key];
  if (!s.stack.length) { el.innerHTML = ""; return; }   // 頂層不顯示麵包屑
  const parts = [`<button class="crumb" data-idx="-1">測試用例</button>`];
  s.stack.forEach((node, i) => {
    parts.push(`<span class="crumb-sep">/</span>`);
    const last = i === s.stack.length - 1;
    parts.push(last
      ? `<span class="crumb cur">${esc(node.label)}</span>`
      : `<button class="crumb" data-idx="${i}">${esc(node.label)}</button>`);
  });
  el.innerHTML = parts.join("");
  el.querySelectorAll(".crumb[data-idx]").forEach((b) =>
    b.onclick = () => popTo(key, Number(b.dataset.idx)));
}

async function loadCases(key) {
  stepCache = {};  // 列表重載（含重跑後）→ 清掉步驟詳情快取，確保 modal 拿到最新結果
  const cfg = CASES[key], s = state[key];
  const lim = s.limit || PAGE;
  const params = new URLSearchParams({ q: s.q || "", limit: lim, offset: s.page * lim });
  if (s.system) params.set("system", s.system);  // 空 = 全部，不帶 system（後端支援 system=None）
  // 下鑽時帶當前層父 id；頂層帶 __root__（後端預設只回頂層用例）
  params.set("parent_id", s.parentId || "__root__");
  renderCrumbs(key);
  if ($("rows")) $("rows").style.opacity = ".45";
  const data = await api("/api/cases?" + params).catch((e) => (toast(e.message), null));
  if (!data || !$("rows")) return;
  renderCaseRows(key, data.items);
  applyLiveProgress(key, data.items);
  setupPager(s, data.total, () => loadCases(key));
}

// ── 執行中用例的進度還原（整頁刷新後 SSE 閉包已不在）────────────────────────
// 後端對 status='running' 的最近 run 附帶 live（由逐步落庫推算的「正在跑哪一步」）：
//   transition → 該 chip 點亮閃爍；wait_api/sleep（≥30s）→ 下一顆 chip 疊「等待中 + 倒數」
//   （截止 = step_started_at + wait_secs）。本頁有活躍 SSE 的用例跳過（SSE 自己會畫）。
// 進度只在「進頁 / 手動刷新 / 自己觸發的 SSE」時更新——不再背景輪詢 /api/cases。
// （過去每 10s 重打清單會打斷正在挑用例 / 滾動 / 勾選的操作人員，已依回饋移除。）
// 倒數 chip 純前端每秒走字，不打後端、不重建表、不清勾選。
let liveTimers = [];
const activeStreams = new Set();   // 本頁正在 runCaseStream 的用例 id
function clearLiveProgress() {
  liveTimers.forEach(clearInterval); liveTimers = [];
}

// 長延時掛起倒數：單一常駐 ticker，每秒把所有 .wc-val[data-resume-at]（清單列 + 詳情抽屜）
// 走字到 resume_at。純前端、不打後端；無倒數元素時閒置不清掉，下次出現續用。
let waitTicker = null;
function ensureWaitTicker() {
  if (waitTicker) return;
  const tick = () => {
    const els = document.querySelectorAll(".wc-val[data-resume-at]");
    if (!els.length) return;
    const now = Math.floor(Date.now() / 1000);
    els.forEach((el) => {
      el.textContent = fmtCountdown((Number(el.getAttribute("data-resume-at")) || 0) - now);
    });
  };
  tick();
  waitTicker = setInterval(tick, 1000);
}
function applyLiveProgress(key, items) {
  clearLiveProgress();
  const fmtLeft = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  for (const c of items) {
    const lr = c.last_result, lv = lr && lr.live;
    if (!lr || lr.status !== "running") continue;
    if (!lv || activeStreams.has(c.id)) continue;
    const row = document.querySelector(`tr[data-id="${CSS.escape(c.id)}"]`);
    if (!row) continue;
    if (lv.kind === "transition") {
      const c = row.querySelector(`.tchip[data-ti="${lv.cur_tindex}"]`);
      if (c) { c.classList.add("tchip-running"); centerChip(c); }
      continue;
    }
    if (!(lv.wait_secs >= 30)) continue;            // 短等待不展示（與 SSE 行為一致）
    const end = (lv.step_started_at + lv.wait_secs) * 1000;
    if (end <= Date.now()) continue;                // 估算已到期（條件可能快滿足）→ 等下輪重載
    const target = lv.next_tindex != null ? row.querySelector(`.tchip[data-ti="${lv.next_tindex}"]`) : null;
    let el = target, base;
    if (el) {
      el.classList.add("tchip-running", "tchip-waiting");
      el.title = `${lv.name}（倒數至逾時上限，條件滿足即提前結束）`;
      base = `${el.textContent} 等待中`;
      centerChip(el);                               // 等待中的下一顆步驟也滾到中央
    } else {
      const flow = row.querySelector(".tflow");
      if (!flow) continue;
      el = document.createElement("span");
      el.className = "tchip tchip-running tchip-wait";
      el.title = lv.name || "";
      flow.appendChild(el);
      base = "⏳ 等待中";
    }
    const tick = () => {
      el.textContent = `${base} ${fmtLeft(Math.max(0, Math.round((end - Date.now()) / 1000)))}`;
    };
    tick();
    liveTimers.push(setInterval(tick, 1000));
  }
}

// 任務流橫向滑動：把正在執行的 chip 滾到 .tflow 可視區中央（不影響整頁捲動）。
// .tflow 已設 position:relative，故 chip.offsetLeft 相對 .tflow 內容左緣。
function centerChip(c) {
  const flow = c && c.closest(".tflow");
  if (!flow) return;
  const target = c.offsetLeft - (flow.clientWidth - c.offsetWidth) / 2;
  flow.scrollTo({ left: Math.max(0, target), behavior: "smooth" });
}
// transition chip 著色 / 最近結果徽章——全量重建（renderCaseRows）與 SSE 逐步更新共用。
function tchipCls(st) {
  return st === "passed" ? "tchip-pass" : st === "failed" ? "tchip-fail"
    : st === "skipped" ? "tchip-skip" : "";
}
// 每個 transition chip 依最近一次執行中對應步驟的結果著色（綠=通過 / 紅=失敗）
function tflowHtml(c) {
  const tss = (c.last_result && c.last_result.transition_status) || [];
  return c.transitions.length
    ? `<div class="tflow">${c.transitions.map((x, i) =>
        `<span class="tchip clickable ${tchipCls(tss[i])}" data-cid="${esc(c.id)}" data-ti="${i}" title="點擊看詳情">${esc(x.split("_")[0])}</span>`).join("")}</div>`
    : `<span class="sub2">db / 混合</span>`;
}
// 失敗時把第一個失敗步驟的錯誤掛在 title、略過時把 skip_reason 掛在 title——滑過徽章
// 即可看原因，不必下鑽；略過另在徽章下方顯示截斷的原因，讓「為什麼略過」一眼可見。
function lrCellHtml(c) {
  const lr = c.last_result;
  // 長延時掛起：徽章下方掛「⏳ 倒數至喚醒」，由 wait ticker 每秒走字（點「查看」看原因）
  if (lr && lr.status === "waiting" && lr.wait) {
    const w = lr.wait;
    const tip = `${w.step_label || "長延時等待"}（預計 ${fmtTs(w.resume_at)} 喚醒；點「查看」看為什麼）`;
    return `<span title="${esc(tip)}">${resBadge("waiting")}</span>
      <div class="sub2 wait-countdown" title="${esc(tip)}">⏳ <span class="wc-val" data-resume-at="${w.resume_at}">…</span></div>`;
  }
  const isSkip = (lr && lr.status === "skipped") || c.skip;
  const lrTip = (lr && lr.error) || (isSkip ? c.skip_reason : "");
  const skipWhy = isSkip && c.skip_reason ? `<div class="sub2 skip-why">${esc(c.skip_reason)}</div>` : "";
  return lr ? `<span${lrTip ? ` title="${esc(lrTip)}"` : ""}>${resBadge(lr.status)}</span>${skipWhy}`
    : (c.skip ? `<span${lrTip ? ` title="${esc(lrTip)}"` : ""}>${resBadge("skipped")}</span>${skipWhy}`
              : `<span class="sub2">—</span>`);
}
function renderCaseRows(key, items) {
  const tb = $("rows");
  if (!tb) return;
  if (!items.length) {
    tb.innerHTML = `<tr class="norow"><td colspan="8"><div class="empty">沒有用例</div></td></tr>`;
    tb.style.opacity = "1"; return;
  }
  tb.innerHTML = items.map((c) => {
    const tflow = tflowHtml(c);
    const lrHtml = lrCellHtml(c);
    return `<tr data-id="${esc(c.id)}">
      <td class="ck-col"><input type="checkbox" class="row-ck" data-id="${esc(c.id)}" /></td>
      <td class="desc-col"><div class="cid">${c.seq != null ? `<span class="seq">#${c.seq}</span>` : ""}<code>${esc(c.id)}</code></div><div class="sub2">${esc((c.description || "").slice(0, 90))}</div></td>
      <td class="src-col"><span class="pill">${c.source === "generated" ? "AI 產生" : "內建"}</span></td>
      <td class="date-col"><span class="sub2">${fmtTs(c.created_at)}</span></td>
      <td class="num">${c.step_count}</td>
      <td class="flow-col">${tflow}</td>
      <td class="lr-col">${lrHtml}</td>
      <td class="act">
        <button class="btn view-btn" data-id="${esc(c.id)}">查看</button>
        <button class="btn run-btn" data-id="${esc(c.id)}">執行</button>
        <button class="btn copy-btn" data-id="${esc(c.id)}">複製</button>
        <button class="btn republish-btn" data-id="${esc(c.id)}">重新發佈</button>
        ${c.child_count > 0 ? `<button class="btn sub-btn" data-id="${esc(c.id)}">子任務(${c.child_count})</button>` : ""}
      </td></tr>`;
  }).join("");
  tb.style.opacity = "1";
  tb.querySelectorAll(".row-ck").forEach((ck) => ck.onchange = updateBatchState);
  updateBatchState();   // 重載清單後同步「批量執行」按鈕與全選框狀態（勾選隨列表重建歸零）
  tb.querySelectorAll(".view-btn").forEach((b) => b.onclick = () => openCaseDetail(b.dataset.id));
  tb.querySelectorAll(".run-btn").forEach((b) => b.onclick = () => runCase(key, b));
  tb.querySelectorAll(".copy-btn").forEach((b) => b.onclick = () => copyCase(key, b));
  tb.querySelectorAll(".republish-btn").forEach((b) => b.onclick = () => republishCase(key, b));
  // 子任務：下鑽到該用例的子清單（遞迴天然成立——子層列同樣會帶 child_count）
  tb.querySelectorAll(".sub-btn").forEach((b) => b.onclick = () => drillInto(key, { id: b.dataset.id }));
  tb.querySelectorAll(".tchip.clickable").forEach((ch) =>
    ch.onclick = () => openStepModal(key, ch.dataset.cid, Number(ch.dataset.ti)));
  ensureWaitTicker();   // 列表若有 waiting 列，啟動倒數走字（idempotent）
}

// 分解輸入框 #N 用例引用高亮：textarea 文字設透明（caret-color 保留游標），
// 由疊在上面的鏡像層（.uc-hl，同字體/內距/換行）顯示文字並把 #N 上藍色。
// 鏡像層整層 pointer-events:none、僅 .uc-ref 可點——點 #N 開用例詳情抽屜
// （後端 /api/cases/<純數字> 以看板序號 seq 回退反查），其餘點擊穿透回 textarea。
// 代價：點 #N 字面無法把游標放進該 token（用方向鍵可進），換取可點擊。
function setupUcRefs() {
  const ta = $("uc"), hl = $("uc-hl");
  if (!ta || !hl) return;
  const paint = () => {
    // 先 esc 再替換（# 與數字不受轉義影響）；尾端補 \n 讓結尾空行也佔高、捲動對齊
    hl.innerHTML = esc(ta.value).replace(/#(\d+)/g,
      `<span class="uc-ref" data-seq="$1" title="點擊查看用例 #$1">#$1</span>`) + "\n";
    hl.scrollTop = ta.scrollTop;
  };
  ta.addEventListener("input", paint);
  ta.addEventListener("scroll", () => { hl.scrollTop = ta.scrollTop; });
  hl.addEventListener("click", (e) => {
    const s = e.target.closest(".uc-ref");
    if (s) openCaseDetail(s.dataset.seq);
  });
  paint();
}

async function openCaseDetail(id) {
  openDrawer(`<div class="empty">載入中…</div>`);
  const d = await api("/api/cases/" + encodeURIComponent(id)).catch((e) => (toast(e.message), null));
  $("drawer-body").innerHTML = d ? caseDetailHtml(d) : `<div class="empty">載入失敗</div>`;
  ensureWaitTicker();   // 詳情若是 waiting，啟動倒數走字（idempotent）
}

// 執行核心：走 SSE（/api/cases/run-stream），邊跑邊即時更新該列 chip——
//   run_start  → 該列所有 chip 還原待跑底色
//   step_start → 對應 chip 點亮「進行中」閃爍（橘）；等待類步驟（wait_api/sleep，無對應
//                chip）在任務流尾端掛暫時的「⏳ 等待中」chip——否則 wait_api 等十幾分鐘
//                （如下班卡等 start_at 到點）看板毫無動靜，像死機
//   step_end   → 依結果為該 chip 上色（綠/紅/灰），局部刷新，不整頁重載；等待 chip 移除
//   run_end    → 局部更新該列「最近結果」欄（drawer 選項開啟時再開抽屜顯示完整結果）
// 回傳 Promise（run_end / error / 連線中斷時 resolve，不 reject），供「執行」按鈕與
// 批量執行共用：批量時串行 await、且 drawer:false 避免逐條彈抽屜。
function runCaseStream(id, row, { drawer = true } = {}) {
  return new Promise((resolve) => {
    // 本頁開跑改由 SSE 畫進度：註冊 activeStreams 讓刷新還原跳過此用例，
    // 並清掉既有的還原計時器（避免兩套計時器互搶 chip 文字）。
    activeStreams.add(id);
    clearLiveProgress();
    const done = (v) => { activeStreams.delete(id); resolve(v); };
    // 列表隨時可能重渲染（新增用例/翻頁/搜尋），閉包抓住的 row 會變成離線節點、
    // 後續事件與計時器全更新到看不見的舊列——一律以 data-id 即時解析「現在掛在 DOM 上的那一列」。
    const liveRow = () => {
      if (!row || !row.isConnected) row = document.querySelector(`tr[data-id="${CSS.escape(id)}"]`);
      return row;
    };
    // 以列為範圍取 chip（data-ti 為 transition 序號，與後端事件的 tindex 對齊）
    const chip = (ti) => {
      const r = liveRow();
      return (ti == null || !r) ? null : r.querySelector(`.tchip[data-ti="${ti}"]`);
    };
    const restoreChip = (c) => {   // 還原被「等待中」裝飾過的 chip 原文字/標題/樣式
      c.textContent = c.dataset.orig || c.textContent;
      if (c.dataset.origTitle != null) c.title = c.dataset.origTitle;
      c.classList.remove("tchip-running", "tchip-waiting");
      delete c.dataset.orig; delete c.dataset.origTitle;
    };
    const setChip = (ti, cls) => {
      const c = chip(ti);
      if (!c) return;
      if (c.classList.contains("tchip-waiting")) restoreChip(c);  // 防禦：殘留的等待裝飾
      c.classList.remove("tchip-running", "tchip-pass", "tchip-fail", "tchip-skip");
      if (cls) c.classList.add(cls);
      if (cls === "tchip-running") centerChip(c);  // 正在執行的步驟滾到任務流中央
    };
    const statusToCls = (st) => st === "passed" ? "tchip-pass" : st === "failed" ? "tchip-fail"
      : st === "skipped" ? "tchip-skip" : "";
    // 等待類步驟（tindex=null）的顯示：優先把「等待中 + 倒數」疊在下一顆 transition chip 上
    // （如「J6 等待中 14:22」），沒有下一顆（等待在最後一個 transition 之後）才退回任務流
    // 尾端掛暫時 chip；<30s 的短等待不展示。倒計時：sleep 是精確秒數；wait_api 倒向逾時上限
    // （條件滿足會提前結束）。狀態存 waitState、每秒 applyWait()——除了倒數，也負責在列表
    // 重渲染後把裝飾重新掛回新節點（自我修復），重渲染最多閃斷 1 秒。
    const fmtLeft = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    let waitState = null, waitTimer = 0;
    const applyWait = () => {
      if (!waitState) return;
      const w = waitState;
      const cd = ` ${fmtLeft(Math.max(0, Math.round((w.end - Date.now()) / 1000)))}`;
      const r = liveRow();
      if (!r) return;                       // 整列暫不在 DOM（重渲染瞬間），下一秒再試
      if (w.nextTi != null) {
        const c = r.querySelector(`.tchip[data-ti="${w.nextTi}"]`);
        if (c) {
          if (!c.classList.contains("tchip-waiting")) {   // 首次或重渲染後的新節點：掛裝飾
            c.dataset.orig = c.textContent;
            c.dataset.origTitle = c.title;
            c.title = w.hint;
            c.classList.add("tchip-running", "tchip-waiting");
            centerChip(c);                               // 等待中的下一顆步驟滾到中央
          }
          c.textContent = `${c.dataset.orig} 等待中${cd}`;
          return;
        }
        // 列在但沒這顆 chip（等待在最後一個 transition 之後）→ 落到尾部暫掛
      }
      const flow = r.querySelector(".tflow");
      if (!flow) return;
      let c = flow.querySelector(".tchip-wait");
      if (!c) {                             // 首次或重渲染後：補回尾部暫時 chip
        c = document.createElement("span");
        c.className = "tchip tchip-running tchip-wait";
        c.title = w.hint;
        flow.appendChild(c);
      }
      c.textContent = `⏳ 等待中${cd}`;
    };
    const removeWaitChip = () => {
      if (waitTimer) { clearInterval(waitTimer); waitTimer = 0; }
      waitState = null;
      const r = liveRow();
      if (!r) return;
      r.querySelectorAll(".tchip-wait").forEach((x) => x.remove());
      r.querySelectorAll(".tchip-waiting").forEach(restoreChip);
    };
    const showWaitChip = (e) => {
      removeWaitChip();
      if (!(e.wait_secs >= 30)) return;   // 短等待（<30s 的 sleep / 快輪詢）不值得展示，免得 chip 閃來閃去
      waitState = {
        nextTi: e.next_tindex,
        end: Date.now() + e.wait_secs * 1000,
        hint: (e.name || "") + (e.kind === "wait_api" ? "（倒數至逾時上限，條件滿足即提前結束）" : ""),
      };
      applyWait();
      waitTimer = setInterval(applyWait, 1000);
    };

    const es = new EventSource("/api/cases/run-stream?id=" + encodeURIComponent(id));
    let startedAt = null;
    es.onmessage = (ev) => {
      let e; try { e = JSON.parse(ev.data); } catch { return; }
      if (e.type === "run_start") {
        startedAt = e.started_at;
        // 還原該列所有 chip 為待跑底色（重跑時清掉上一輪殘留的綠/紅與等待 chip）
        removeWaitChip();
        const r0 = liveRow();
        if (r0) r0.querySelectorAll(".tchip").forEach((c) =>
          c.classList.remove("tchip-running", "tchip-pass", "tchip-fail", "tchip-skip"));
        // 「最近結果」欄立即標「執行中」——若等到 run_end 才更新，執行期間此欄一直是
        // 上一輪的舊結果；同時若清單裡另有早前啟動、仍在長等待的 run（如打卡用例），
        // 只有那列顯示「執行中」，看起來就像執行中跑錯列
        updateLastResultCell(r0, { status: "running" });
      } else if (e.type === "step_start") {
        if (e.tindex == null) {
          if (e.kind === "wait_api" || e.kind === "sleep") showWaitChip(e);
        } else setChip(e.tindex, "tchip-running"); // 進行中：閃爍
      } else if (e.type === "step_end") {
        if (e.tindex == null) removeWaitChip();
        else setChip(e.tindex, statusToCls(e.status)); // 完成：上色（局部刷新）
      } else if (e.type === "run_end") {
        removeWaitChip();
        es.close();
        delete stepCache[id];                     // 清步驟詳情快取 → chip 點擊 modal 取最新結果
        if (e.skipped) toast(`${id}：略過${e.skip_reason ? "（" + e.skip_reason + "）" : ""}`);
        else toast(`${id}：${e.status === "passed" ? "通過" : "失敗"}（${e.passed}/${e.total}）`);
        updateLastResultCell(liveRow(), e, startedAt);  // 局部更新「最近結果」欄，不整頁重載
        if (drawer) showRunResultDrawer(id, e);   // 開抽屜顯示完整結果
        done({ ok: true, status: e.status, skipped: !!e.skipped });
      } else if (e.type === "run_suspend") {
        // 長延時掛起：不是失敗也不是結束——已冷凍成 waiting，交給 resume_worker 到點喚醒。
        // 關掉 SSE（否則 EventSource 會重連→重跑），把「最近結果」欄改成 等待中 + 倒數。
        removeWaitChip();
        es.close();
        delete stepCache[id];
        toast(`${id}：長延時掛起，已交給 resume_worker（預計 ${fmtTs(e.resume_at)} 喚醒）`);
        const r = liveRow();
        const act = r && r.querySelector("td.act");
        const cell = act && act.previousElementSibling;
        if (cell) {
          cell.innerHTML = `${resBadge("waiting")}
            <div class="sub2 wait-countdown" title="點「查看」看為什麼等這麼久">⏳ <span class="wc-val" data-resume-at="${e.resume_at}">…</span></div>`;
          ensureWaitTicker();
        }
        done({ ok: true, status: "waiting" });
      } else if (e.type === "error") {
        removeWaitChip();
        es.close();
        toast("執行失敗：" + (e.error || "未知錯誤"));
        done({ ok: false });
      }
    };
    es.onerror = () => {
      removeWaitChip();
      es.close(); toast("執行中斷：與看板的連線已斷開"); done({ ok: false });
    };
  });
}

// 「執行」按鈕：單條執行（行為與既往一致——按鈕鎖定 + 完整結果抽屜）
async function runCase(key, btn) {
  const id = btn.dataset.id, old = btn.textContent;
  const row = btn.closest("tr");
  btn.disabled = true; btn.textContent = "執行中…";
  toast(`執行中：${id}（登入 + 呼叫被測 API，請稍候）`);
  await runCaseStream(id, row);
  btn.disabled = false; btn.textContent = old;
}

// ── 勾選 + 批量執行（主用例與子任務層通用）──────────────────────────────────
// 同步「批量執行」按鈕（啟用態 + 已勾數）與表頭全選框（全勾 / 半勾）狀態
function updateBatchState() {
  const btn = $("batch-run");
  if (!btn) return;   // 防禦：按鈕不在 DOM（頁面切走）時略過
  const all = document.querySelectorAll(".row-ck");
  const checked = document.querySelectorAll(".row-ck:checked");
  if (!btn.dataset.running) {
    btn.disabled = checked.length === 0;
    btn.textContent = checked.length ? `▶ 批量執行（${checked.length}）` : "▶ 批量執行";
  }
  const ca = $("ck-all");
  if (ca) {
    ca.checked = all.length > 0 && checked.length === all.length;
    ca.indeterminate = checked.length > 0 && checked.length < all.length;
  }
}

// 批量執行：勾選的用例（主用例或子任務皆可）**並行**跑（上限 BATCH_CONCURRENCY），
// 每條 run 後端會以唯一租約配發互斥的帳號組（_run_spec 的 lease_owner），不會互搶帳號。
// 並行上限的考量：① 帳號池容量——每條 job 用例租 1 商家 + 3~5 夥伴，池就 7 商家 11 夥伴；
// ② 瀏覽器同域 HTTP/1.1 連線數上限（約 6），每條 run 佔一條 SSE。
// 每條沿用單條執行的 SSE 即時 chip 上色與「最近結果」局部更新；不逐條彈抽屜，
// 全部跑完 toast 彙總。執行期間鎖定批量按鈕、各列「執行」按鈕與勾選框，避免重入。
const BATCH_CONCURRENCY = 3;
async function batchRun(key) {
  const btn = $("batch-run");
  const picked = Array.from(document.querySelectorAll(".row-ck:checked"));
  if (!picked.length) { toast("請先勾選要執行的用例"); return; }
  const lockEls = document.querySelectorAll(".row-ck, #ck-all, .run-btn, .republish-btn");
  btn.dataset.running = "1"; btn.disabled = true;
  lockEls.forEach((el) => { el.disabled = true; });
  const conc = Math.min(BATCH_CONCURRENCY, picked.length);
  toast(`批量執行中：共 ${picked.length} 條用例（${conc} 條並行、各用獨立帳號）`);
  let pass = 0, fail = 0, skip = 0, done = 0, next = 0;
  btn.textContent = `批量執行中 0/${picked.length}…`;
  const worker = async () => {
    while (next < picked.length) {
      const i = next++;                 // 單執行緒 JS，無競態
      const id = picked[i].dataset.id, row = picked[i].closest("tr");
      const r = await runCaseStream(id, row, { drawer: false });
      if (r.ok && r.skipped) skip++;
      else if (r.ok && r.status === "passed") pass++;
      else fail++;
      done++;
      btn.textContent = `批量執行中 ${done}/${picked.length}…`;
    }
  };
  await Promise.all(Array.from({ length: conc }, worker));
  toast(`批量執行完成：通過 ${pass} / 失敗 ${fail}${skip ? ` / 略過 ${skip}` : ""}（共 ${picked.length} 條）`);
  delete btn.dataset.running;
  lockEls.forEach((el) => { el.disabled = false; });
  updateBatchState();
}

// run_end 後局部更新該列「最近結果」欄（td.act 前一格），免整頁重載即反映本次結果
function updateLastResultCell(row, e, startedAt) {
  if (!row) return;
  const act = row.querySelector("td.act");
  const cell = act && act.previousElementSibling;
  if (!cell) return;
  cell.innerHTML = resBadge(e.status);
}

// run_end 後開抽屜顯示完整步驟結果：直接抓該用例最新 steps（剛落地）渲染，與重試/換號收尾一致
async function showRunResultDrawer(id, e) {
  try {
    const d = await api("/api/cases/" + encodeURIComponent(id));
    const r = d.last_result;
    openDrawer(`<div class="dhead"><h3>執行結果 · ${esc(id)} ${resBadge(e.status)}</h3></div>
      <div class="sec">${r ? runResultHtml(r) : "（無步驟）"}</div>`);
  } catch (_) { /* 抽屜僅輔助，失敗不影響已即時更新的 chip */ }
}

// 以既有用例 spec 為範本快速再建一條新用例（不含執行歷史），刷新後新列因序號（seq）最大排到最前
async function copyCase(key, btn) {
  const id = btn.dataset.id, old = btn.textContent;
  btn.disabled = true; btn.textContent = "複製中…";
  try {
    const res = await apiPost("/api/cases/copy", { id });
    toast(`已複製為 ${res.id}`);
    loadCases(key);
  } catch (e) { toast("複製失敗：" + e.message); }
  finally { btn.disabled = false; btn.textContent = old; }
}

// 重新發佈：以該用例 spec 為範本複製成新 id 後「立即執行」，讓時間綁定用例每次發佈都
// 落成一筆全新獨立記錄（執行歷史掛在新 id 下，完全不動原用例與其歷史）。仿 runCase：
// disabled + toast 提示 → POST → 成功 toast + 開抽屜顯示這次發佈的執行結果 → 刷新清單
// （新列序號（seq）最大會排到最前）。
async function republishCase(key, btn) {
  const id = btn.dataset.id, old = btn.textContent;
  btn.disabled = true; btn.textContent = "發佈中…";
  toast(`重新發佈中：${id}（複製成新記錄 + 登入 + 呼叫被測 API，請稍候）`);
  try {
    const res = await apiPost("/api/cases/republish", { id });
    const r = res.result;
    const pass = r.steps.filter((x) => x.status === "passed").length;
    toast(`已重新發佈為 ${res.id}：${r.status === "passed" ? "通過" : "失敗"}（${pass}/${r.steps.length}）`);
    openDrawer(`<div class="dhead"><h3>重新發佈結果 · ${esc(res.id)} ${resBadge(r.status)}</h3>
      <p class="sub2">由 ${esc(id)} 複製成新記錄並立即執行（不牽連原用例歷史）</p></div>
      <div class="sec">${runResultHtml(r)}</div>`);
    loadCases(key);
  } catch (e) { toast("重新發佈失敗：" + e.message); }
  finally { btn.disabled = false; btn.textContent = old; }
}

// ── 任務流 chip → 步驟詳情 modal ────────────────────────────────────────────
let stepCache = {};  // cid → steps[]（列表重載時清空）

const stepStatusBadge = (st) => st === "passed" ? `<span class="badge b-done">通過</span>`
  : st === "failed" ? `<span class="badge b-failed">失敗</span>`
  : st === "skipped" ? `<span class="badge b-draft">略過</span>`
  : `<span class="badge b-draft">未執行</span>`;

const jsonBlock = (obj) => `<pre class="yaml">${esc(JSON.stringify(obj, null, 2))}</pre>`;

function stepModalHtml(s, idx, total, runId) {
  const cls = s.result ? (s.result.status === "passed" ? "tchip-pass"
    : s.result.status === "failed" ? "tchip-fail" : "tchip-skip") : "";
  const r = s.result;
  const sec = (title, body) => body ? `<div class="sec"><h4>${title}</h4>${body}</div>` : "";
  const obs = r && r.observations && Object.keys(r.observations).length ? jsonBlock(r.observations) : "";
  const resultBody = r
    ? `<div class="sub2" style="margin-bottom:8px">${stepStatusBadge(r.status)}
         ${r.started_at ? `<span class="sub2">· 執行於 ${fmtTsS(r.started_at)}</span>` : ""}
         ${r.elapsed_ms != null ? `<span class="sub2">· 耗時 ${r.elapsed_ms}ms</span>` : ""}</div>
       ${r.error ? `<div class="err">${esc(r.error)}</div>` : ""}
       ${obs ? `<div class="sub2" style="margin:6px 0 4px">observations</div>${obs}` : ""}`
    : `<div class="sub2">（此用例尚無執行記錄，下面僅顯示規格）</div>`;
  return `<div class="step-modal">
    <div class="sm-head">
      <div class="sm-nav">
        <button class="btn ghost" id="step-prev" ${idx <= 0 ? "disabled" : ""}>‹ 上一步</button>
        <span class="sub2">步驟 ${idx + 1} / ${total}${runId ? ` · <code>${esc(runId)}</code>` : ""}</span>
        <button class="btn ghost" id="step-next" ${idx >= total - 1 ? "disabled" : ""}>下一步 ›</button>
      </div>
      <h3><span class="tchip ${cls}">${esc(s.short)}</span> ${esc(s.name)} ${r ? stepStatusBadge(r.status) : ""}</h3>
      <p class="sub2">
        ${s.method ? `<b>${esc(s.method)}</b> <code>${esc(s.endpoint || "")}</code>` : ""}
        ${s.doc_id ? ` · 文件 ${esc(String(s.doc_id))}` : ""}
        ${s.actor ? ` · 操作者 ${esc(s.actor)}` : ""}</p>
      ${s.summary ? `<p class="sub2">${esc(s.summary)}</p>` : ""}
    </div>
    ${sec("執行結果", resultBody)}
    ${r && r.status === "failed" ? `<div class="sec sm-actions">
      <h4>失敗處理（AI 協助）</h4>
      <div class="sm-act-row">
        <button class="btn" id="sm-analyze">🔍 分析</button>
        <button class="btn" id="sm-feedback">💬 意見反饋</button>
        <button class="btn" id="sm-retry">↻ 重試</button>
        <button class="btn" id="sm-swap">⇄ 換一個號</button>
      </div>
      <div class="sm-fix" id="sm-fix"></div>
    </div>` : ""}
    ${sec("Request", s.request ? jsonBlock(s.request) : "")}
    ${sec("預期（expect）", s.expect && Object.keys(s.expect).length ? jsonBlock(s.expect) : "")}
    ${sec("DB 副作用（side_effects）", s.side_effects ? jsonBlock(s.side_effects) : "")}
    ${sec("推播（push）", s.push ? jsonBlock(s.push) : "")}
  </div>`;
}

function showStep(key, cid, data, idx) {
  const steps = data.steps;
  idx = Math.max(0, Math.min(idx, steps.length - 1));
  openModal(stepModalHtml(steps[idx], idx, steps.length, data.run_id));
  const p = $("step-prev"), n = $("step-next");
  if (p) p.onclick = () => showStep(key, cid, data, idx - 1);
  if (n) n.onclick = () => showStep(key, cid, data, idx + 1);
  // 失敗步驟才有的三顆按鈕：分析（AI 診斷，不自動執行）/ 重試（整支重跑）/ 換一個號（換池中同能力帳號重跑）
  const aBtn = $("sm-analyze");
  if (aBtn) aBtn.onclick = async () => {
    const fix = $("sm-fix"); fix.innerHTML = `<div class="sub2">AI 分析中…</div>`;
    aBtn.disabled = true;
    try {
      const d = await apiPost("/api/cases/analyze", { id: cid, step_index: idx });
      fix.innerHTML = analysisHtml(d);
      const fxBtn = $("sm-ai-fix");
      if (fxBtn) fxBtn.onclick = () => submitAiFix(fxBtn, cid, data, idx, d);
    }
    catch (e) { fix.innerHTML = `<div class="err">分析失敗：${esc(e.message)}</div>`; }
    finally { aBtn.disabled = false; }
  };
  const rBtn = $("sm-retry");
  if (rBtn) rBtn.onclick = () => rerunStep(key, cid, idx, "/api/cases/run", { id: cid }, "重試");
  const sBtn = $("sm-swap");
  if (sBtn) sBtn.onclick = () => rerunStep(key, cid, idx, "/api/cases/swap-account", { id: cid, step_index: idx }, "換號");
  // 意見反饋：輸入框 → 發送 → 帶用例關鍵信息建一條 feedback 標記，交後台 worker 自動修復流程
  const fBtn = $("sm-feedback");
  if (fBtn) fBtn.onclick = () => openFeedbackForm(cid, data, idx);
}

// 意見反饋：在 sm-fix 區展開輸入框；發送時把「用例關鍵信息 + 用戶意見」組成 content，
// 建一條 kind=feedback 的標記（POST /api/markups），後台 markup worker 會以
// 「修復測試流程」視角自動處理（改用例 YAML / endpoints 規格 / 框架代碼）。
function openFeedbackForm(cid, data, idx) {
  const fix = $("sm-fix");
  if (!fix) return;
  fix.innerHTML = `
    <textarea id="sm-fb-text" rows="3" placeholder="描述這次失敗的問題或修復建議（將連同用例關鍵信息一併送出）…"></textarea>
    <div class="sm-act-row" style="margin-top:8px">
      <button class="btn primary" id="sm-fb-send">發送</button>
      <button class="btn ghost" id="sm-fb-cancel">取消</button>
    </div>`;
  const ta = $("sm-fb-text"); ta?.focus();
  $("sm-fb-cancel").onclick = () => { fix.innerHTML = ""; };
  $("sm-fb-send").onclick = async () => {
    const text = (ta.value || "").trim();
    if (!text) { toast("請先填寫反饋內容"); ta.focus(); return; }
    const s = data.steps[idx] || {}, r = s.result || {};
    const content = [
      "【用例執行失敗 — 意見反饋】",
      `用例：${cid}`,
      `失敗步驟：[${idx + 1}/${data.steps.length}] ${s.name || s.short || ""}`,
      s.endpoint ? `端點：${s.method || ""} ${s.endpoint}` : "",
      data.run_id ? `run_id：${data.run_id}` : "",
      r.error ? `錯誤：${r.error}` : "",
      "",
      "── 用戶意見 ──",
      text,
    ].filter((x) => x !== "").join("\n");
    const btn = $("sm-fb-send"); btn.disabled = true; btn.textContent = "發送中…";
    try {
      await apiPost("/api/markups", { kind: "feedback", route: "cases", content });
      fix.innerHTML = `<div class="sub2">✓ 反饋已送出，後台 worker 會根據意見自動修復流程（可在「標記」頁追蹤進度）</div>`;
      toast("意見反饋已送出");
    } catch (e) {
      toast("發送失敗：" + e.message);
      btn.disabled = false; btn.textContent = "發送";
    }
  };
}

// AI 診斷結果渲染：根因 + 推理 + 建議 + 建議動作標籤 + AI修復按鈕
// （按鈕綁定在 sm-analyze handler 裡——這裡是純模板，拿不到 cid/step 上下文）
function analysisHtml(d) {
  const actLabel = { retry: "建議：重試", swap: "建議：換一個號", inspect: "建議：人工檢查", report: "疑似主倉 bug，建議回報" };
  return `<div class="ai-analysis">
    <div class="aa-cause">🔍 <b>${esc(d.cause || "")}</b></div>
    ${d.detail ? `<p class="sub2">${esc(d.detail)}</p>` : ""}
    ${d.suggestion ? `<p class="sub2">💡 ${esc(d.suggestion)}</p>` : ""}
    <div class="aa-action"><span class="pill">${esc(actLabel[d.recommended_action] || d.recommended_action || "")}</span>
      <button class="btn mini primary" id="sm-ai-fix" title="把分析結果提交為標記，後台 worker 依建議自動修復">🤖 AI修復</button></div>
  </div>`;
}

// AI修復：把「AI 分析結果 + 用例關鍵信息」組成 content，建一條 kind=feedback 標記
// （與意見反饋同一條自動修復管線，差別只是內容由分析結果自動生成、免手填）。
async function submitAiFix(btn, cid, data, idx, d) {
  const s = data.steps[idx] || {}, r = s.result || {};
  const content = [
    "【用例執行失敗 — AI修復】",
    `用例：${cid}`,
    `失敗步驟：[${idx + 1}/${data.steps.length}] ${s.name || s.short || ""}`,
    s.endpoint ? `端點：${s.method || ""} ${s.endpoint}` : "",
    data.run_id ? `run_id：${data.run_id}` : "",
    r.error ? `錯誤：${r.error}` : "",
    "",
    "── AI 分析 ──",
    d.cause ? `根因：${d.cause}` : "",
    d.detail ? `推理：${d.detail}` : "",
    d.suggestion ? `建議：${d.suggestion}` : "",
    d.recommended_action ? `建議動作：${d.recommended_action}` : "",
    "",
    "請依上述分析自動修復（改用例 YAML / endpoints 規格 / 框架代碼；不要動被測主倉），修復後說明改了什麼。",
  ].filter((x) => x !== "").join("\n");
  btn.disabled = true; btn.textContent = "提交中…";
  try {
    await apiPost("/api/markups", { kind: "feedback", route: "cases", content });
    btn.textContent = "✓ 已提交";
    toast("AI修復標記已送出，後台 worker 將自動處理（「標記」頁可追蹤進度）");
  } catch (e) {
    toast("提交失敗：" + e.message);
    btn.disabled = false; btn.textContent = "🤖 AI修復";
  }
}

// 重試 / 換號：真打被測 API + 寫 DB（整支重跑），完成後清快取、刷新清單、重開同一步顯示新結果
async function rerunStep(key, cid, idx, url, body, label) {
  const fix = $("sm-fix");
  if (fix) fix.innerHTML = `<div class="sub2">${label}中：登入 + 呼叫被測 API，請稍候…</div>`;
  toast(`${label}中：${cid}`);
  try {
    const res = await apiPost(url, body);
    const run = res.result || res;   // swap 回 {result, swapped}；run 直接回 RunResult
    const steps = run.steps || [];
    const pass = steps.filter((x) => x.status === "passed").length;
    if (res.swapped) toast(`已換號 ${res.swapped.actor}：${res.swapped.from} → ${res.swapped.to || "（無可用替補）"}`);
    toast(`${label}結果：${run.status === "passed" ? "通過" : "失敗"}（${pass}/${steps.length}）`);
    delete stepCache[cid];               // 清快取 → 重新抓最新步驟結果
    if (key) loadCases(key);             // 同步刷新清單列的最近結果
    await openStepModal(key, cid, idx);  // 重開同一步，顯示重跑後的結果
  } catch (e) {
    if (fix) fix.innerHTML = `<div class="err">${label}失敗：${esc(e.message)}</div>`;
    else toast(`${label}失敗：` + e.message);
  }
}

async function openStepModal(key, cid, idx) {
  let data = stepCache[cid];
  if (!data) {
    openModal(`<div class="empty">載入中…</div>`);
    const d = await api(`/api/cases/${encodeURIComponent(cid)}/steps`).catch((e) => (toast(e.message), null));
    if (!d || !d.steps) { closeModal(); return; }
    data = stepCache[cid] = d;
  }
  showStep(key, cid, data, idx);
}

// 分解後任務流步驟清單（沿用 cstep 樣式）；plan.steps 來自 preview 回傳
function decomposeStepsHtml(plan) {
  return (plan.steps || []).map((st, i) => {
    const lbl = st.kind === "db_exec" ? "db_exec" : esc(st.transition || "?");
    return `<div class="cstep"><span class="ci">${i}</span>
      <span class="badge ${st.kind === "db_exec" ? "b-draft" : "b-running"}">${lbl}</span>
      ${st.note ? `<span class="sub2">${esc(st.note)}</span>` : ""}</div>`;
  }).join("");
}

// 掃描 spec YAML 中與「時間 / 打卡碼 / db_exec 參數」相關的行，醒目列出供使用者核對。
// 重點是讓「1 小時後」這類語意能被人眼驗證有沒有被正確翻譯（含 UNIX_TIMESTAMP / +3600 /
// start_at / end_at / 日期字樣 / 打卡碼等）。回傳要塞進提示區的 HTML（無命中回空字串）。
const TIME_HINT_RE = /UNIX_TIMESTAMP|start_at|end_at|start_code|end_code|\+\s*3600|\+\s*\d{3,}|\bNOW\(\)|\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}:\d{2}|DATE_ADD|INTERVAL/i;
function timeHintsHtml(specYaml) {
  const hits = String(specYaml || "").split("\n")
    .map((ln) => ln.trim())
    .filter((ln) => ln && TIME_HINT_RE.test(ln));
  if (!hits.length) {
    return `<div class="sub2">（未偵測到明顯的時間 / 打卡碼參數；若用例與時間相關，請手動核對下方 YAML）</div>`;
  }
  return `<div class="time-hints">${hits.map((ln) =>
    `<div class="thint"><code>${esc(ln)}</code></div>`).join("")}</div>`;
}

// 落地成功後的結果抽屜（preview 確認 + commit 後共用呈現）
function showDecomposeResult(d, plan, run) {
  openDrawer(`
    <div class="dhead"><span class="sn">${esc(d.saved)} · ${esc(d.system)}</span>
      <h3>${esc((plan && plan.path_id) || d.spec.id || "")} <span class="pill">AI 產生</span></h3>
      <p class="sub2">${esc((plan && plan.description) || d.spec.description || "")}</p></div>
    <div class="sec"><h4>任務流</h4>${plan ? decomposeStepsHtml(plan) : ""}</div>
    ${d.result ? `<div class="sec"><h4>執行結果 ${resBadge(d.result.status)}</h4>${runResultHtml(d.result)}</div>` : ""}
    ${(d.children && d.children.length) ? `<div class="sec"><h4>子用例（${d.children.length} 條）</h4>
      ${d.children.map((c) => `<div class="cstep"><code>${esc(c.id)}</code>${c.skip ? ` <span class="pill">skip</span>` : ""}</div>`).join("")}</div>` : ""}`);
  // 子用例數量附在 toast 尾（k 條子用例已掛在主用例底下，主列會自動顯示「子任務(k)」）
  const kids = (d.children && d.children.length) ? `（+ ${d.children.length} 條子用例）` : "";
  toast((run ? `已建立並執行：${d.result ? (d.result.status === "passed" ? "通過" : "失敗") : ""}`
    : "已建立，存入 generated/") + kids);
}

// 子用例區塊：列出 preview 分析出的子用例（分支 / 邊界 / 負向），可逐條勾選一併建立。
// 非 skip 者預設勾選；skip 者顯示其 skip_reason 並預設不勾（作為可見的覆蓋缺口，仍可手動勾）。
// children 為空時回空字串（彈窗不顯示此區塊，行為與 #1 完全一致，不退化）。
function childrenSectionHtml(pv) {
  const children = pv.children || [];
  if (!children.length) return "";
  // 被截斷時明示「已分析 N 條，顯示前 M 條」，呼應「不要靜默截斷」原則
  const trunc = pv.children_truncated
    ? `<span class="sub2">（已分析 ${pv.children_analyzed} 條，顯示前 ${children.length} 條）</span>` : "";
  const rows = children.map((c, i) => {
    const checked = c.skip ? "" : "checked";
    const reason = c.skip
      ? `<div class="sub2 dc-child-skip">⚠ 暫無法自動化：${esc(c.skip_reason || "")}（仍可勾選一併建立，作為可見的覆蓋缺口）</div>` : "";
    return `<label class="dc-child">
      <input type="checkbox" class="dc-child-ck" data-i="${i}" ${checked} />
      <span class="dc-child-body">
        <code>${esc(c.id || "")}</code>${c.skip ? ` <span class="pill">skip</span>` : ""}
        <div class="sub2">${esc(c.description || "")}</div>${reason}
      </span></label>`;
  }).join("");
  return `<div class="sec"><h4>子用例（將一併建立）${trunc}</h4>
    <p class="sub2">AI 分析主流程中有分支的步驟，衍生出以下負向 / 邊界子用例（掛在主用例底下，預設不執行）。</p>
    <div class="dc-children">${rows}</div></div>`;
}

// 確認彈窗：展示 preview 出的關鍵參數 + 時間提示 + 可編輯 spec YAML + 子用例勾選，
// 「確定建立」才送 commit；「取消」直接關閉、不留任何記錄。
function openConfirmModal(key, pv, run) {
  const plan = pv.plan || {};
  openModal(`<div class="dc-confirm">
    <h3>確認生成參數 <span class="pill">尚未建立</span></h3>
    <p class="sub2">以下為 AI 分解結果，<b>尚未落地</b>。請核對（特別是時間語意），可直接編輯下方 YAML 後再建立；取消則不留任何記錄。</p>
    <div class="sec"><h4>用例</h4>
      <div class="sub2"><code>${esc(pv.proposed_id || "")}</code> · ${esc(pv.system || "")}</div>
      <p class="sub2">${esc(plan.description || "")}</p></div>
    <div class="sec"><h4>任務流（${(plan.steps || []).length} 步）</h4>${decomposeStepsHtml(plan)}</div>
    <div class="sec"><h4>⏱ 時間 / 打卡碼 / db_exec 參數（請確認「${esc((plan.description || "").slice(0, 20))}」的時間語意是否正確）</h4>
      <div id="dc-hints">${timeHintsHtml(pv.spec_yaml)}</div></div>
    <div class="sec"><h4>spec YAML（可編輯校正）</h4>
      <textarea id="dc-yaml" class="dc-yaml" rows="16" spellcheck="false">${esc(pv.spec_yaml || "")}</textarea></div>
    ${childrenSectionHtml(pv)}
    <label class="ck"><input type="checkbox" id="dc-run" ${run ? "checked" : ""} /> 建立後立即執行</label>
    <div class="dc-confirm-actions">
      <button class="btn ghost" id="dc-cancel">取消</button>
      <button class="btn primary" id="dc-ok">確定建立</button>
    </div>
  </div>`);
  // YAML 編輯後即時刷新時間提示，協助使用者邊改邊核對時間參數
  const ta = $("dc-yaml");
  if (ta) ta.oninput = () => { const h = $("dc-hints"); if (h) h.innerHTML = timeHintsHtml(ta.value); };
  $("dc-cancel").onclick = closeModal;  // 取消：不送任何東西、不留記錄
  const children = pv.children || [];
  const ok = $("dc-ok");
  ok.onclick = async () => {
    const specYaml = $("dc-yaml").value;
    const doRun = $("dc-run").checked;  // checkbox 語意：建立後立即執行（只在 commit 帶上）
    // 收集「勾選保留」的子用例 spec_yaml（只送勾選的；未勾選的不落地）
    const picked = [];
    document.querySelectorAll(".dc-child-ck").forEach((ck) => {
      if (ck.checked) { const c = children[Number(ck.dataset.i)]; if (c) picked.push(c.spec_yaml); }
    });
    const oldTxt = ok.textContent; ok.disabled = true;
    ok.textContent = doRun ? "建立 + 執行中…" : "建立中…";
    try {
      const d = await apiPost("/api/cases/decompose/commit",
        { spec_yaml: specYaml, run: doRun, children: picked });
      closeModal();
      showDecomposeResult(d, plan, doRun);
      loadCases(key);
    } catch (e) {
      toast("建立失敗：" + e.message);
      ok.disabled = false; ok.textContent = oldTxt;
    }
  };
}

// AI 用例分解彈窗：把原本常駐頁首的分解面板搬進 modal（騰出清單高度）。
// 沿用當前 tab 的 placeholder/system；開啟後掛 #N 引用高亮（setupUcRefs 需 DOM 已就位）。
function openDecomposeModal(key) {
  const s = state[key] || {}, cur = tabByKey(s.tab);
  openModal(`<div class="uc-modal">
    <h3 class="modal-title">✨ AI 用例分解 <span class="sub2">（${esc(cur.label)}）</span></h3>
    <p class="sub2">自然語言用例 → DeepSeek 分解成任務流（preview 確認後才存入 generated/）。可用 #N 引用既有用例。</p>
    <div class="ai-form">
      <div class="uc-wrap">
        <textarea id="uc" rows="4" placeholder="${esc(cur.ph)}"></textarea>
        <div class="uc-hl" id="uc-hl" aria-hidden="true"></div>
      </div>
      <div class="uc-modal-actions">
        <label class="ck"><input type="checkbox" id="uc-run" /> 建立後立即執行</label>
        <button class="btn ghost" id="uc-cancel">取消</button>
        <button class="btn primary" id="uc-go">分解</button>
      </div>
    </div>
  </div>`);
  $("uc-cancel").onclick = closeModal;
  $("uc-go").onclick = () => doDecompose(key);
  setupUcRefs();
  const ta = $("uc"); if (ta) ta.focus();
}

async function doDecompose(key) {
  const uc = $("uc").value.trim();
  if (!uc) { toast("請先輸入用例"); return; }
  const s = state[key] || {}, cur = tabByKey(s.tab);
  // 規劃中領域（labor/employer）：前端先攔，給明確提示，不送後端
  if (cur.planned) { toast(`${cur.label}領域分解規劃中，暫不支援`); return; }
  // uc-run checkbox 語意改為「建立後立即執行」，先讀起來帶進確認彈窗（只在 commit 時生效）
  const run = $("uc-run").checked, btn = $("uc-go"), old = btn.textContent;
  btn.disabled = true; btn.textContent = "分解中…";
  try {
    // 第一段：只 preview（產 plan/spec 但不落地），帶上當前 tab 的 system（all 為 ""）
    const pv = await apiPost("/api/cases/decompose/preview", { use_case: uc, system: cur.system });
    // 第二段：彈窗確認/校正，使用者按「確定建立」才 commit 落地（取消不留記錄）
    openConfirmModal(key, pv, run);
  } catch (e) { toast("分解失敗：" + e.message); }
  finally { btn.disabled = false; btn.textContent = old; }
}
