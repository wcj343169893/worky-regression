"use strict";
// 測試用例（工作 / 任務）：列用例 + 執行 + AI 用例分解。

import { $, api, apiPost, esc, fmtTs, resBadge, toast, PAGE, state } from "./util.js";
import { setupPager, openDrawer, openModal, closeModal } from "./widgets.js";

export const CASES = {
  "job-cases": { title: "工作測試用例", system: "job",
    ph: "例：商家發工作，夥伴申請後商家取消錄取" },
  "task-cases": { title: "任務測試用例", system: "contract",
    ph: "例：發案者發任務，接案者申請、發案者同意，最後完成並通過" },
};

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

export async function renderCases(key) {
  const cfg = CASES[key];
  const s = state[key] || (state[key] = { q: "", page: 0 });
  $("view").innerHTML = `
    <div class="cases-page">
      <div class="view-head"><h2>${esc(cfg.title)}</h2>
        <span class="sub2">讀 cases/*.yaml（含 AI 產生的 generated/），依建立時間倒序；對應 results/ 顯示最近一次執行</span></div>
      <div class="card ai-panel">
        <div class="panel-head"><h3>AI 用例分解</h3>
          <span class="sub2">自然語言用例 → DeepSeek 分解成任務流（存入 generated/）</span></div>
        <div class="ai-form">
          <textarea id="uc" rows="2" placeholder="${esc(cfg.ph)}"></textarea>
          <label class="ck"><input type="checkbox" id="uc-run" /> 分解後立即執行</label>
          <button class="btn primary" id="uc-go">分解</button>
        </div>
      </div>
      <div class="card cases-list">
        <div class="panel-head"><h3>用例清單</h3>
          <input type="search" id="q" placeholder="搜尋 名稱 / 描述…" value="${esc(s.q)}" /></div>
        <div class="table-wrap"><table>
          <thead><tr><th>用例</th><th>來源</th><th>建立時間</th><th class="num">步驟</th><th>任務流</th><th>最近結果</th><th class="act">操作</th></tr></thead>
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
  let t; $("q").oninput = (e) => { clearTimeout(t); s.q = e.target.value; t = setTimeout(() => { s.page = 0; loadCases(key); }, 300); };
  $("uc-go").onclick = () => doDecompose(key);
  loadCases(key);
}

async function loadCases(key) {
  stepCache = {};  // 列表重載（含重跑後）→ 清掉步驟詳情快取，確保 modal 拿到最新結果
  const cfg = CASES[key], s = state[key];
  const params = new URLSearchParams({ system: cfg.system, q: s.q || "", limit: PAGE, offset: s.page * PAGE });
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
      <td><div class="strong">${esc(c.id)}</div><div class="sub2">${esc((c.description || "").slice(0, 50))}</div></td>
      <td><span class="pill">${c.source === "generated" ? "AI 產生" : "內建"}</span></td>
      <td><span class="sub2">${fmtTs(c.created_at)}</span></td>
      <td class="num">${c.step_count}</td>
      <td>${tflow}</td>
      <td>${lrHtml}</td>
      <td class="act">
        <button class="btn view-btn" data-id="${esc(c.id)}">查看</button>
        <button class="btn run-btn" data-id="${esc(c.id)}">執行</button>
      </td></tr>`;
  }).join("");
  tb.style.opacity = "1";
  tb.querySelectorAll(".view-btn").forEach((b) => b.onclick = () => openCaseDetail(b.dataset.id));
  tb.querySelectorAll(".run-btn").forEach((b) => b.onclick = () => runCase(key, b));
  tb.querySelectorAll(".tchip.clickable").forEach((ch) =>
    ch.onclick = () => openStepModal(ch.dataset.cid, Number(ch.dataset.ti)));
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
    ${sec("Request", s.request ? jsonBlock(s.request) : "")}
    ${sec("預期（expect）", s.expect && Object.keys(s.expect).length ? jsonBlock(s.expect) : "")}
    ${sec("DB 副作用（side_effects）", s.side_effects ? jsonBlock(s.side_effects) : "")}
    ${sec("推播（push）", s.push ? jsonBlock(s.push) : "")}
  </div>`;
}

function showStep(cid, data, idx) {
  const steps = data.steps;
  idx = Math.max(0, Math.min(idx, steps.length - 1));
  openModal(stepModalHtml(steps[idx], idx, steps.length, data.run_id));
  const p = $("step-prev"), n = $("step-next");
  if (p) p.onclick = () => showStep(cid, data, idx - 1);
  if (n) n.onclick = () => showStep(cid, data, idx + 1);
}

async function openStepModal(cid, idx) {
  let data = stepCache[cid];
  if (!data) {
    openModal(`<div class="empty">載入中…</div>`);
    const d = await api(`/api/cases/${encodeURIComponent(cid)}/steps`).catch((e) => (toast(e.message), null));
    if (!d || !d.steps) { closeModal(); return; }
    data = stepCache[cid] = d;
  }
  showStep(cid, data, idx);
}

async function doDecompose(key) {
  const uc = $("uc").value.trim();
  if (!uc) { toast("請先輸入用例"); return; }
  const run = $("uc-run").checked, btn = $("uc-go"), old = btn.textContent;
  btn.disabled = true; btn.textContent = run ? "分解 + 執行中…" : "分解中…";
  try {
    const d = await apiPost("/api/cases/decompose", { use_case: uc, run });
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
