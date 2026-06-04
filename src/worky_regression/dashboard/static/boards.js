"use strict";
// 看板引擎（工作 / 任務共用，左中右三欄）：統計分布 + 清單 + 詳情抽屜。

import {
  $, api, esc, money, fmtTs, fmtDate8, laborName, shopName, empName,
  badge, resBadge, contractCat, CAT_COLOR, toast, PAGE, state, OPT,
} from "./util.js";

// 最近執行結果 / 執行次數（Issue #1：把看板綁回測試框架）。沿用用例頁 resBadge 風格。
function lastRunCell(r) {
  const lr = r.last_run;
  if (!lr) return `<span class="sub2">—</span>`;
  return `${resBadge(lr.status)}<div class="sub2">${fmtTs(lr.started_at)} · ${lr.runs}次</div>`;
}
import { filterBar, bindFilters, applyFilterParams, fillRows, openDrawer, f, mini } from "./widgets.js";

export const BOARDS = {
  jobs: {
    title: "工作清單", statsUrl: "/api/job-stats", listUrl: "/api/jobs",
    detailUrl: (sn) => "/api/jobs/" + encodeURIComponent(sn), filterParam: "category",
    chipVal: (c) => c.category, entryCat: (c) => c.category, snOf: (r) => r.job_sn,
    filters: [
      { key: "pay_status", options: OPT.payStatus },
      { key: "payment_method_id", options: OPT.payMethod },
      { key: "wage_min", type: "num", label: "時薪≥" },
      { key: "wage_max", type: "num", label: "時薪≤" },
    ],
    columns: [
      ["工作", (r) => `<div class="strong">${esc(r.name || "(未命名)")}</div><div class="mono">${esc(r.job_sn)}</div>`],
      ["進度", (r) => badge(r.progress.category, r.progress.status_label)],
      ["付款", (r) => `<span class="sub2">${esc(r.pay_status_label)}</span>`],
      ["時薪", (r) => money(r.hourly_wage), "num"],
      ["預估金額", (r) => money(r.estimated_total_amount), "num"],
      ["付款方式", (r) => `<span class="sub2">${esc(r.payment_method_label)}</span>`],
      ["招募", (r) => `${r.recruited_count}/${r.recruit_count} <span class="sub2">申 ${r.apply_count}</span>`],
      ["時段", (r) => `${fmtDate8(r.start_date)}<div class="sub2">${esc(r.start_time_period || "")}–${esc(r.end_time_period || "")}</div>`],
      ["商家/店鋪", (r) => `${empName(r.employer)}<div class="sub2">${shopName(r.shop)}</div>`],
      ["最近執行", lastRunCell],
      ["更新", (r) => `<span class="sub2">${fmtTs(r.updated_at)}</span>`],
    ],
    detail: jobDetailHtml,
  },
  tasks: {
    title: "任務清單（承攬制）", statsUrl: "/api/stats", listUrl: "/api/tasks",
    detailUrl: (sn) => "/api/tasks/" + encodeURIComponent(sn), filterParam: "progress",
    chipVal: (c) => c.code, entryCat: (c) => contractCat(c.code), snOf: (r) => r.task_sn,
    filters: [
      { key: "pay_status", options: OPT.payStatus },
      { key: "payment_method_id", options: OPT.payMethod },
    ],
    columns: [
      ["任務", (r) => `<div class="strong">${esc(r.name || "(未命名)")}</div><div class="mono">${esc(r.task_sn)}</div>`],
      ["進度", (r) => badge(contractCat(r.progress.code), r.progress.title)],
      ["付款", (r) => `<span class="sub2">${esc(r.pay_status_label)}</span>`],
      ["金額", (r) => money(r.task_amount), "num"],
      ["付款方式", (r) => `<span class="sub2">${esc(r.payment_method_label)}</span>`],
      ["招募", (r) => `${r.recruited_count}/${r.recruit_count}`],
      ["時段", (r) => `${fmtTs(r.start_at)}<div class="sub2">~ ${fmtTs(r.end_at)}</div>`],
      ["發案者", (r) => laborName(r.publisher)],
      ["接案者", (r) => laborName(r.receiver)],
      ["最近執行", lastRunCell],
      ["更新", (r) => `<span class="sub2">${fmtTs(r.updated_at)}</span>`],
    ],
    detail: taskDetailHtml,
  },
};

export async function renderBoard(key) {
  const cfg = BOARDS[key];
  const s = state[key] || (state[key] = { q: "", page: 0, category: "", selSn: null, filters: {} });
  s.filters = s.filters || {};
  $("view").innerHTML = `
    <div class="board">
      <aside class="col-left">
        <div class="stats-v" id="stats"></div>
        <div class="card dist">
          <div class="panel-head"><h3>進度分布</h3><span class="sub2" id="dist-total"></span></div>
          <div class="dist-bar" id="dist-bar"></div>
          <div class="dist-legend" id="dist-legend"></div>
        </div>
      </aside>
      <main class="col-center">
        <div class="card">
          <div class="panel-head">
            <h3>${esc(cfg.title)}</h3>
            <div class="filters">
              <input type="search" id="q" placeholder="搜尋 編號 / 名稱…" value="${esc(s.q)}" />
              ${filterBar(cfg, s)}
            </div>
          </div>
          <div class="table-wrap"><table>
            <thead><tr>${cfg.columns.map((c) => `<th class="${c[2] === "num" ? "num" : ""}">${c[0]}</th>`).join("")}${cfg.detail ? `<th class="act">操作</th>` : ""}</tr></thead>
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
      </main>
    </div>`;

  // 左欄統計 + 進度分布：整頁渲染時抓一次；翻頁/搜尋/篩選只 ajax 重抓清單
  const stats = await api(cfg.statsUrl).catch((e) => (toast(e.message), null));
  if (stats) {
    $("stats").innerHTML = [
      ["總數", stats.total, ""], ["進行中", stats.active, ""],
      ["已完成", stats.completed, "ok"], ["取消/失敗", stats.canceled, "warn"],
    ].map(([k, v, c]) => `<div class="stat ${c}"><div class="k">${k}</div><div class="v">${v.toLocaleString()}</div></div>`).join("");
    $("dist-total").textContent = `共 ${stats.total.toLocaleString()}`;
    const segs = stats.by_progress.filter((c) => c.count > 0);
    $("dist-bar").innerHTML = segs.map((c) => {
      const cat = cfg.entryCat(c), w = stats.total ? (c.count / stats.total * 100) : 0;
      return `<div class="dist-seg" style="width:${w}%;background:${CAT_COLOR[cat] || "#5d6b8c"}" title="${esc(c.title)} ${c.count}"></div>`;
    }).join("");
    $("dist-legend").innerHTML = [{ title: "全部", count: stats.total, _all: true }]
      .concat(stats.by_progress).map((c) => {
        const val = c._all ? "" : cfg.chipVal(c);
        const cat = c._all ? null : cfg.entryCat(c);
        const dot = c._all ? "" : `<span class="dot" style="background:${CAT_COLOR[cat] || "#5d6b8c"}"></span>`;
        return `<div class="row ${String(s.category) === String(val) ? "active" : ""}" data-v="${val}">${dot}<span>${esc(c.title)}</span><span class="n">${c.count.toLocaleString()}</span></div>`;
      }).join("");
    $("dist-legend").querySelectorAll(".row").forEach((el) =>
      el.onclick = () => {
        s.category = el.dataset.v; s.page = 0;
        $("dist-legend").querySelectorAll(".row").forEach((r) =>
          r.classList.toggle("active", r === el));
        loadBoardList(key);
      });
  }

  let t; $("q").oninput = (e) => { clearTimeout(t); s.q = e.target.value; t = setTimeout(() => { s.page = 0; loadBoardList(key); }, 350); };
  bindFilters(cfg, s, () => loadBoardList(key));
  loadBoardList(key);
}

async function loadBoardList(key) {
  const cfg = BOARDS[key], s = state[key];
  const params = new URLSearchParams({ q: s.q, limit: PAGE, offset: s.page * PAGE });
  if (s.category !== "") params.set(cfg.filterParam, s.category);
  applyFilterParams(params, cfg, s);
  if ($("rows")) $("rows").style.opacity = ".45";
  const data = await api(cfg.listUrl + "?" + params).catch((e) => (toast(e.message), null));
  if (!data || !$("rows")) return;
  fillRows(cfg, data, s, () => loadBoardList(key), (sn, tr) => selectRow(key, cfg, sn, tr));
}

async function selectRow(key, cfg, sn, tr) {
  const s = state[key]; s.selSn = sn;
  document.querySelectorAll("#rows tr").forEach((el) => el.classList.toggle("sel", el === tr));
  openDrawer(`<div class="empty">載入中…</div>`);
  const d = await api(cfg.detailUrl(sn)).catch((e) => (toast(e.message), null));
  $("drawer-body").innerHTML = d ? cfg.detail(d) : `<div class="empty">載入失敗</div>`;
}

function jobDetailHtml(d) {
  const j = d.job;
  return `
    <div class="dhead"><span class="sn">${esc(j.job_sn)}</span>
      <h3>${esc(j.name || "(未命名)")} ${badge(j.progress.category, j.progress.status_label)}</h3></div>
    <div class="sec"><h4>工作資訊</h4>
      ${f("狀態", esc(j.status_label) + ` <span class="sub2">(${j.status})</span>`)}
      ${f("付款", esc(j.pay_status_label))}
      ${f("時薪 / 預估", money(j.hourly_wage) + " / " + money(j.estimated_total_amount))}
      ${f("付款方式", esc(j.payment_method_label))}
      ${f("招募", `${j.recruited_count}/${j.recruit_count}（申請 ${j.apply_count}）`)}
      ${f("日期", fmtDate8(j.start_date) + " " + esc(j.start_time_period || "") + "–" + esc(j.end_time_period || ""))}
      ${f("商家 / 店鋪", empName(j.employer) + " / " + shopName(j.shop))}
      ${f("地址", esc(d.address || "-"))}
      ${f("發佈 / 更新", fmtTs(j.published_at) + " / " + fmtTs(j.updated_at))}
    </div>
    <div class="sec"><h4>打工夥伴記錄（s_labor_jobs）</h4>
      ${mini(["夥伴", "上工", "執行", "打卡", "時薪"], d.labor_jobs.map((r) => [
        laborName(r.labor), esc(r.status_label), esc(r.job_status_label),
        ((r.actual_clock_in_at ? "✓進" : "") + (r.actual_clock_out_at ? " ✓出" : "")) || "-",
        money(r.wage)]))}
    </div>
    <div class="sec"><h4>申請 / 媒合（s_labor_match_jobs）</h4>
      ${mini(["夥伴", "狀態", "時間"], d.match_jobs.map((r) => [
        laborName(r.labor), esc(r.status_label), fmtTs(r.created_at)]))}
    </div>`;
}

function taskDetailHtml(d) {
  const t = d.task;
  return `
    <div class="dhead"><span class="sn">${esc(t.task_sn)}</span>
      <h3>${esc(t.name || "(未命名)")} ${badge(contractCat(t.progress.code), t.progress.title)}</h3></div>
    <div class="sec"><h4>任務資訊</h4>
      ${f("進度", esc(t.progress.title))}
      ${f("狀態 / 付款", esc(t.status_label) + " / " + esc(t.pay_status_label))}
      ${f("金額", money(t.task_amount))}
      ${f("招募", `${t.recruited_count}/${t.recruit_count}`)}
      ${f("時段", fmtTs(t.start_at) + " – " + fmtTs(t.end_at))}
      ${f("發案者 / 接案者", laborName(t.publisher) + " / " + laborName(t.receiver))}
      ${f("對應 transition", esc(t.progress.transition || "-"))}
    </div>
    <div class="sec"><h4>進度時間軸</h4>
      ${mini(["事件", "時間"], (d.timeline || []).map((r) => [esc(r.status_label), fmtTs(r.created_at)]))}
    </div>`;
}
