"use strict";
// 測試用例（工作 / 任務）：列用例 + 執行 + AI 用例分解。

import { $, api, apiPost, esc, fmtTs, resBadge, toast, PAGE, state } from "./util.js";
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
  { key: "labor", label: "打工夥伴", system: "labor", planned: true,
    ph: "打工夥伴帳號生命週期（註冊 / 審核…）— 規劃中，暫不支援分解" },
  { key: "employer", label: "商家", system: "employer", planned: true,
    ph: "商家建立店鋪 / 審核等流程 — 規劃中，暫不支援分解" },
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
      <h3>${esc(d.id)} <span class="pill">${d.source === "generated" ? "AI 產生" : "內建"}</span></h3>
      <p class="sub2">${esc(d.description || "")}</p></div>
    <div class="sec"><h4>任務流（${d.steps.length} 步）</h4>${stepList}</div>
    <div class="sec"><h4>最近執行結果</h4>
      ${last ? `<div class="sub2" style="margin-bottom:8px">${resBadge(last.status)} ${fmtTs(last.started_at)}${last.run_id ? ` · <code>${esc(last.run_id)}</code>` : ""}</div>${runResultHtml(last)}`
        : `<div class="sub2">（尚無執行記錄）</div>`}</div>
    <div class="sec"><h4>YAML</h4><pre class="yaml">${esc(d.yaml)}</pre></div>`;
}

export async function renderCases(key, tabKey) {
  const cfg = CASES[key];
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
  const cur = tabByKey(s.tab);
  // 內建 + 自訂 tab 依序渲染；自訂 tab 帶可移除的 ✕；末尾再接「＋新增」按鈕
  const tabsHtml = allTabs().map((t) =>
    `<button class="dc-tab${t.key === cur.key ? " active" : ""}" data-tab="${esc(t.key)}">${esc(t.label)}` +
    `${t.planned ? `<span class="dc-soon">規劃中</span>` : ""}` +
    `${t.custom ? `<span class="dc-del" data-del="${esc(t.key)}" title="移除此領域">✕</span>` : ""}</button>`
  ).join("") +
    `<button class="dc-tab dc-add" id="dc-add" title="用一句話描述，AI 自動建立領域 tab">＋ 新增</button>`;
  $("view").innerHTML = `
    <div class="cases-page">
      <div class="view-head"><h2>${esc(cfg.title)}</h2>
        <span class="sub2">讀 cases/*.yaml（含 AI 產生的 generated/），依建立時間倒序；對應 results/ 顯示最近一次執行</span></div>
      <div class="card ai-panel">
        <div class="panel-head"><h3>AI 用例分解</h3>
          <span class="sub2">自然語言用例 → DeepSeek 分解成任務流（存入 generated/）</span></div>
        <div class="dc-tabs">${tabsHtml}</div>
        <div class="ai-form">
          <textarea id="uc" rows="2" placeholder="${esc(cur.ph)}"></textarea>
          <label class="ck"><input type="checkbox" id="uc-run" /> 分解後立即執行</label>
          <button class="btn primary" id="uc-go">分解</button>
        </div>
      </div>
      <div class="card cases-list">
        <div class="crumbs" id="crumbs"></div>
        <div class="panel-head"><h3>用例清單</h3>
          <input type="search" id="q" placeholder="搜尋 名稱 / 描述…" value="${esc(s.q)}" /></div>
        <div class="table-wrap"><table>
          <thead><tr><th>用例 ID / 描述</th><th>來源</th><th>建立時間</th><th class="num">步驟</th><th>任務流</th><th>最近結果</th><th class="act">操作</th></tr></thead>
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
  // 搜尋：回首頁並回到頂層（清空下鑽堆疊），讓搜尋語意一致
  let t; $("q").oninput = (e) => { clearTimeout(t); s.q = e.target.value; t = setTimeout(() => { resetToRoot(s); loadCases(key); }, 300); };
  $("uc-go").onclick = () => doDecompose(key);
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

// ── 主任務/子任務下鑽：堆疊 + 麵包屑 ────────────────────────────────────────
// 回到頂層：清空下鑽堆疊與當前父 id，並回到第一頁
function resetToRoot(s) { s.stack = []; s.parentId = null; s.page = 0; }

// 下鑽進某用例的子清單：把當前層 push 進堆疊，切到該用例為新父層，回第一頁
function drillInto(key, c) {
  const s = state[key];
  s.stack.push({ id: c.id, label: c.id });
  s.parentId = c.id;
  s.page = 0;
  loadCases(key);
}

// 麵包屑點某層：pop 到該層（idx = -1 代表回頂層「測試用例」）
function popTo(key, idx) {
  const s = state[key];
  s.stack = s.stack.slice(0, idx + 1);
  s.parentId = s.stack.length ? s.stack[s.stack.length - 1].id : null;
  s.page = 0;
  loadCases(key);
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
  const params = new URLSearchParams({ q: s.q || "", limit: PAGE, offset: s.page * PAGE });
  if (s.system) params.set("system", s.system);  // 空 = 全部，不帶 system（後端支援 system=None）
  // 下鑽時帶當前層父 id；頂層帶 __root__（後端預設只回頂層用例）
  params.set("parent_id", s.parentId || "__root__");
  renderCrumbs(key);
  if ($("rows")) $("rows").style.opacity = ".45";
  const data = await api("/api/cases?" + params).catch((e) => (toast(e.message), null));
  if (!data || !$("rows")) return;
  renderCaseRows(key, data.items);
  setupPager(s, data.total, () => loadCases(key));
}

function renderCaseRows(key, items) {
  const tb = $("rows");
  if (!tb) return;
  if (!items.length) {
    tb.innerHTML = `<tr class="norow"><td colspan="7"><div class="empty">沒有用例</div></td></tr>`;
    tb.style.opacity = "1"; return;
  }
  tb.innerHTML = items.map((c) => {
    const lr = c.last_result;
    // 每個 transition chip 依最近一次執行中對應步驟的結果著色（綠=通過 / 紅=失敗）
    const tss = (lr && lr.transition_status) || [];
    const tchipCls = (st) => st === "passed" ? "tchip-pass" : st === "failed" ? "tchip-fail"
      : st === "skipped" ? "tchip-skip" : "";
    const tflow = c.transitions.length
      ? `<div class="tflow">${c.transitions.map((x, i) =>
          `<span class="tchip clickable ${tchipCls(tss[i])}" data-cid="${esc(c.id)}" data-ti="${i}" title="點擊看詳情">${esc(x.split("_")[0])}</span>`).join("")}</div>`
      : `<span class="sub2">db / 混合</span>`;
    const lrHtml = lr ? `${resBadge(lr.status)} <span class="sub2">${lr.passed}/${lr.total} · ${fmtTs(lr.started_at)}</span>`
      : `<span class="sub2">—</span>`;
    return `<tr>
      <td><div class="cid">${c.seq != null ? `<span class="seq">#${c.seq}</span>` : ""}<code>${esc(c.id)}</code></div><div class="sub2">${esc((c.description || "").slice(0, 50))}</div></td>
      <td><span class="pill">${c.source === "generated" ? "AI 產生" : "內建"}</span></td>
      <td><span class="sub2">${fmtTs(c.created_at)}</span></td>
      <td class="num">${c.step_count}</td>
      <td>${tflow}</td>
      <td>${lrHtml}</td>
      <td class="act">
        <button class="btn view-btn" data-id="${esc(c.id)}">查看</button>
        <button class="btn run-btn" data-id="${esc(c.id)}">執行</button>
        ${c.child_count > 0 ? `<button class="btn sub-btn" data-id="${esc(c.id)}">子任務(${c.child_count})</button>` : ""}
      </td></tr>`;
  }).join("");
  tb.style.opacity = "1";
  tb.querySelectorAll(".view-btn").forEach((b) => b.onclick = () => openCaseDetail(b.dataset.id));
  tb.querySelectorAll(".run-btn").forEach((b) => b.onclick = () => runCase(key, b));
  // 子任務：下鑽到該用例的子清單（遞迴天然成立——子層列同樣會帶 child_count）
  tb.querySelectorAll(".sub-btn").forEach((b) => b.onclick = () => drillInto(key, { id: b.dataset.id }));
  tb.querySelectorAll(".tchip.clickable").forEach((ch) =>
    ch.onclick = () => openStepModal(key, ch.dataset.cid, Number(ch.dataset.ti)));
}

async function openCaseDetail(id) {
  openDrawer(`<div class="empty">載入中…</div>`);
  const d = await api("/api/cases/" + encodeURIComponent(id)).catch((e) => (toast(e.message), null));
  $("drawer-body").innerHTML = d ? caseDetailHtml(d) : `<div class="empty">載入失敗</div>`;
}

async function runCase(key, btn) {
  const id = btn.dataset.id, old = btn.textContent;
  btn.disabled = true; btn.textContent = "執行中…";
  toast(`執行中：${id}（登入 + 呼叫被測 API，請稍候）`);
  try {
    const res = await apiPost("/api/cases/run", { id });
    const pass = res.steps.filter((x) => x.status === "passed").length;
    toast(`${id}：${res.status === "passed" ? "通過" : "失敗"}（${pass}/${res.steps.length}）`);
    openDrawer(`<div class="dhead"><h3>執行結果 · ${esc(id)} ${resBadge(res.status)}</h3></div>
      <div class="sec">${runResultHtml(res)}</div>`);
    loadCases(key);
  } catch (e) { toast("執行失敗：" + e.message); }
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
         ${r.elapsed_ms != null ? `<span class="sub2">· ${r.elapsed_ms}ms</span>` : ""}</div>
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
    try { fix.innerHTML = analysisHtml(await apiPost("/api/cases/analyze", { id: cid, step_index: idx })); }
    catch (e) { fix.innerHTML = `<div class="err">分析失敗：${esc(e.message)}</div>`; }
    finally { aBtn.disabled = false; }
  };
  const rBtn = $("sm-retry");
  if (rBtn) rBtn.onclick = () => rerunStep(key, cid, idx, "/api/cases/run", { id: cid }, "重試");
  const sBtn = $("sm-swap");
  if (sBtn) sBtn.onclick = () => rerunStep(key, cid, idx, "/api/cases/swap-account", { id: cid, step_index: idx }, "換號");
}

// AI 診斷結果渲染：根因 + 推理 + 建議 + 建議動作標籤（純顯示，不自動觸發）
function analysisHtml(d) {
  const actLabel = { retry: "建議：重試", swap: "建議：換一個號", inspect: "建議：人工檢查", report: "疑似主倉 bug，建議回報" };
  return `<div class="ai-analysis">
    <div class="aa-cause">🔍 <b>${esc(d.cause || "")}</b></div>
    ${d.detail ? `<p class="sub2">${esc(d.detail)}</p>` : ""}
    ${d.suggestion ? `<p class="sub2">💡 ${esc(d.suggestion)}</p>` : ""}
    <div class="aa-action"><span class="pill">${esc(actLabel[d.recommended_action] || d.recommended_action || "")}</span></div>
  </div>`;
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

async function doDecompose(key) {
  const uc = $("uc").value.trim();
  if (!uc) { toast("請先輸入用例"); return; }
  const s = state[key] || {}, cur = tabByKey(s.tab);
  // 規劃中領域（labor/employer）：前端先攔，給明確提示，不送後端
  if (cur.planned) { toast(`${cur.label}領域分解規劃中，暫不支援`); return; }
  const run = $("uc-run").checked, btn = $("uc-go"), old = btn.textContent;
  btn.disabled = true; btn.textContent = run ? "分解 + 執行中…" : "分解中…";
  try {
    // 帶上當前 tab 的 system（all 為 ""，後端視為不指定）
    const d = await apiPost("/api/cases/decompose", { use_case: uc, run, system: cur.system });
    const plan = d.plan || {};
    const steps = (plan.steps || []).map((st, i) => {
      const lbl = st.kind === "db_exec" ? "db_exec" : esc(st.transition || "?");
      return `<div class="cstep"><span class="ci">${i}</span>
        <span class="badge ${st.kind === "db_exec" ? "b-draft" : "b-running"}">${lbl}</span>
        ${st.note ? `<span class="sub2">${esc(st.note)}</span>` : ""}</div>`;
    }).join("");
    openDrawer(`
      <div class="dhead"><span class="sn">${esc(d.saved)} · ${esc(d.system)}</span>
        <h3>${esc(plan.path_id || "")} <span class="pill">AI 產生</span></h3>
        <p class="sub2">${esc(plan.description || "")}</p></div>
      <div class="sec"><h4>分解後任務流</h4>${steps}</div>
      ${d.result ? `<div class="sec"><h4>執行結果 ${resBadge(d.result.status)}</h4>${runResultHtml(d.result)}</div>` : ""}`);
    toast(run ? `分解完成並執行：${d.result ? (d.result.status === "passed" ? "通過" : "失敗") : ""}`
      : "分解完成，已存入 generated/");
    loadCases(key);
  } catch (e) { toast("分解失敗：" + e.message); }
  finally { btn.disabled = false; btn.textContent = old; }
}
