"use strict";

// ── helpers ───────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const api = async (path) => {
  const r = await fetch(path);
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || ("HTTP " + r.status));
  return j;
};
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
const pad = (n) => String(n).padStart(2, "0");
function fmtTs(v) {
  if (!v) return "-";
  const d = new Date(Number(v) * 1000);
  if (isNaN(d)) return "-";
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtDate8(v) {
  const s = String(v || "");
  return s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : (s || "-");
}
const money = (n) => (n == null ? "-" : "NT$" + Number(n).toLocaleString());
const laborName = (o) => (!o ? "-" : esc(o.phone || o.username || ("#" + o.id)));
const shopName = (o) => (!o ? "-" : esc(o.name || o.branch_name || ("#" + o.id)));
const empName = (o) => (!o ? "-" : esc(o.phone || ("#" + o.id)));
const stars = (n) => (n == null ? "-" : `<span class="stars">★ ${Number(n).toFixed(1)}</span>`);
const flag = (v, on = "是", off = "否") =>
  v ? `<span class="flag-on">${on}</span>` : `<span class="flag-off">${off}</span>`;
function toast(msg) { const t = $("toast"); t.textContent = msg; t.hidden = false; clearTimeout(t._t); t._t = setTimeout(() => (t.hidden = true), 2600); }
const PAGE = 20;

const CAT_COLOR = { matching: "#5b8cff", recruited: "#2bd4c0", running: "#ffb454",
  done: "#3ddc97", failed: "#ff6b6b", canceled: "#5d6b8c", draft: "#5d6b8c" };

// 承攬制進度碼 → 色塊分類
function contractCat(code) {
  return ({ 1: "matching", 2: "matching", 3: "recruited", 4: "recruited",
    5: "running", 6: "running", 7: "done", 8: "failed", 9: "failed", 10: "canceled" }[code]) || "draft";
}
function badge(cat, label) { return `<span class="badge b-${cat}">${esc(label)}</span>`; }

// ── 條件篩選列（看板與管理表共用）──────────────────────────────────────────
function filterBar(cfg, s) {
  return (cfg.filters || []).map((flt) => {
    const val = s.filters[flt.key] ?? "";
    if (flt.type === "num")
      return `<input type="number" class="flt" id="flt-${flt.key}" placeholder="${esc(flt.label)}" value="${esc(val)}" />`;
    const opts = flt.options.map(([v, l]) =>
      `<option value="${v}" ${String(val) === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("");
    return `<select class="flt" id="flt-${flt.key}">${opts}</select>`;
  }).join("");
}
function bindFilters(cfg, s, rerender) {
  (cfg.filters || []).forEach((flt) => {
    const el = $("flt-" + flt.key);
    if (!el) return;
    const apply = () => { s.filters[flt.key] = el.value; s.page = 0; rerender(); };
    if (flt.type === "num") { let t; el.oninput = () => { clearTimeout(t); t = setTimeout(apply, 450); }; }
    else el.onchange = apply;
  });
}
function applyFilterParams(params, cfg, s) {
  (cfg.filters || []).forEach((flt) => {
    const v = s.filters[flt.key];
    if (v != null && v !== "") params.set(flt.key, v);
  });
}
const OPT = {
  payStatus: [["", "全部付款狀態"], ["0", "無"], ["1", "等待付款"], ["2", "付款完成"], ["3", "準備結算"], ["4", "已結算"], ["5", "結算失敗"], ["6", "待母單結算"], ["7", "付款中"], ["31", "自動取消"]],
  payMethod: [["", "全部付款方式"], ["1", "FunPoint信用卡"], ["2", "信用卡"], ["3", "ATM"]],
  profileComplete: [["", "個資完成?"], ["1", "已完成"], ["0", "未完成"]],
  paymentLocked: [["", "付款鎖定?"], ["1", "已鎖定"], ["0", "未鎖定"]],
  shopValidStatus: [["", "全部驗證狀態"], ["0", "草稿"], ["1", "已送審"], ["2", "審理中"], ["3", "已通過"], ["4", "未通過"]],
  shopValidType: [["", "全部驗證類型"], ["0", "未填寫"], ["1", "統一編號"], ["2", "身分證號"]],
};

// ── 選單 ──────────────────────────────────────────────────────────────────
const NAV = [
  { key: "jobs", label: "工作看板" },
  { key: "tasks", label: "任務看板" },
  { key: "labors", label: "打工夥伴管理" },
  { key: "employers", label: "商家管理" },
  { key: "shops", label: "店鋪管理" },
  { key: "settings", label: "系統設置" },
];
const state = {}; // per-view {q, page, category, selSn}

// ── 看板引擎（工作 / 任務共用，左中右三欄）──────────────────────────────────
const BOARDS = {
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
      ["更新", (r) => `<span class="sub2">${fmtTs(r.updated_at)}</span>`],
    ],
    detail: taskDetailHtml,
  },
};

// 渲染清單列 + 翻頁狀態（看板 / 管理表共用）；只動 #rows 與 pager，不重建外殼
function fillRows(cfg, data, s, key) {
  const tb = $("rows");
  if (!tb) return;
  if (!data.items.length) {
    tb.innerHTML = `<tr class="norow"><td colspan="${cfg.columns.length}"><div class="empty">沒有符合的資料</div></td></tr>`;
  } else {
    const clickable = !!cfg.detail;
    tb.innerHTML = data.items.map((r) => {
      const sn = clickable ? cfg.snOf(r) : null;
      const cls = clickable ? (sn === s.selSn ? "sel" : "") : "norow";
      const attr = clickable ? ` data-sn="${esc(sn)}"` : "";
      return `<tr${attr} class="${cls}">` +
        cfg.columns.map((c) => `<td class="${c[2] === "num" ? "num" : ""}">${c[1](r)}</td>`).join("") + "</tr>";
    }).join("");
    if (clickable) tb.querySelectorAll("tr[data-sn]").forEach((tr) =>
      tr.onclick = () => selectRow(key, cfg, tr.dataset.sn, tr));
  }
  tb.style.opacity = "1";
  const totalPages = Math.max(1, Math.ceil(data.total / PAGE));
  const reload = BOARDS[key] ? () => loadBoardList(key) : () => loadTableList(key);
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

async function renderBoard(key) {
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
  fillRows(cfg, data, s, key);
}

async function selectRow(key, cfg, sn, tr) {
  const s = state[key]; s.selSn = sn;
  document.querySelectorAll("#rows tr").forEach((el) => el.classList.toggle("sel", el === tr));
  openDrawer(`<div class="empty">載入中…</div>`);
  const d = await api(cfg.detailUrl(sn)).catch((e) => (toast(e.message), null));
  $("drawer-body").innerHTML = d ? cfg.detail(d) : `<div class="empty">載入失敗</div>`;
}
function openDrawer(html) {
  $("drawer-body").innerHTML = html || "";
  $("drawer").classList.add("open"); $("drawer-mask").classList.add("open");
}
function closeDrawer() {
  $("drawer").classList.remove("open"); $("drawer-mask").classList.remove("open");
}

// 詳情區塊組件
function f(k, v) { return `<div class="f"><span class="k">${k}</span><span class="v">${v}</span></div>`; }
function mini(headers, rows) {
  if (!rows.length) return `<div class="sub2">（無）</div>`;
  return `<table class="minitable"><thead><tr>${headers.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
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

// ── 管理表格（打工夥伴 / 商家 / 店鋪，整頁）──────────────────────────────────
const TABLES = {
  labors: {
    title: "打工夥伴管理", url: "/api/labors", ph: "搜尋 手機 / 帳號 / ID…",
    filters: [{ key: "is_profile_complete", options: OPT.profileComplete }],
    columns: [
      ["ID", (r) => `<span class="mono">${r.id}</span>`],
      ["手機", (r) => esc(r.phone)],
      ["帳號", (r) => `<span class="sub2">${esc(r.username || "-")}</span>`],
      ["狀態", (r) => `<span class="pill">${r.status}</span>`],
      ["審核", (r) => `<span class="pill">${r.valid_status}</span>`],
      ["評分", (r) => `${stars(r.rating_stars)} <span class="sub2">(${r.evaluation_count})</span>`],
      ["接案/取消/未到", (r) => `${r.job_count} / ${r.canceled_count} / ${r.no_show_count}`, "num"],
      ["扣分", (r) => r.penalty_points, "num"],
      ["最後登入", (r) => `<span class="sub2">${fmtTs(r.last_login_at)}</span>`],
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
    columns: [
      ["ID", (r) => `<span class="mono">${r.id}</span>`],
      ["店鋪", (r) => `<div class="strong">${esc(r.name || "-")}</div><div class="sub2">${esc(r.branch_name || "")}</div>`],
      ["商家ID", (r) => `<span class="mono">${r.employer_id}</span>`],
      ["地區", (r) => `${r.city || "-"} ${r.district || ""}`],
      ["驗證", (r) => `<span class="pill">type ${r.validation_type} / st ${r.validation_status}</span>`],
      ["工作數", (r) => `${r.job_count} <span class="sub2">已發 ${r.published_job_count}</span>`, "num"],
      ["評分", (r) => `${stars(r.rating_stars)} <span class="sub2">(${r.evaluation_count})</span>`],
      ["建立", (r) => `<span class="sub2">${fmtTs(r.created_at)}</span>`],
    ],
  },
};

async function renderTable(key) {
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
    <div class="card"><div class="table-wrap"><table>
      <thead><tr>${cfg.columns.map((c) => `<th class="${c[2] === "num" ? "num" : ""}">${c[0]}</th>`).join("")}</tr></thead>
      <tbody id="rows"></tbody>
    </table></div></div>
    <div class="pager">
      <button class="btn ghost" id="first">« 首頁</button>
      <button class="btn ghost" id="prev">‹ 上一頁</button>
      <span id="pginfo"></span>
      <button class="btn ghost" id="next">下一頁 ›</button>
      <button class="btn ghost" id="last">尾頁 »</button>
    </div>`;
  let t; $("q").oninput = (e) => { clearTimeout(t); s.q = e.target.value; t = setTimeout(() => { s.page = 0; loadTableList(key); }, 350); };
  bindFilters(cfg, s, () => loadTableList(key));
  loadTableList(key);
}

async function loadTableList(key) {
  const cfg = TABLES[key], s = state[key];
  const params = new URLSearchParams({ q: s.q, limit: PAGE, offset: s.page * PAGE });
  applyFilterParams(params, cfg, s);
  if ($("rows")) $("rows").style.opacity = ".45";
  const data = await api(cfg.url + "?" + params).catch((e) => (toast(e.message), null));
  if (!data || !$("rows")) return;
  fillRows(cfg, data, s, key);
}

// ── 系統設置 ────────────────────────────────────────────────────────────────
async function renderSettings() {
  const d = await api("/api/settings").catch((e) => (toast(e.message), null));
  if (!d) return;
  const kv = (k, v) => `<div class="kv"><span class="key">${k}</span><span class="val">${v}</span></div>`;
  const dot = d.deepseek_key_set ? `<span class="dot-ok">● 已設定</span>` : `<span class="dot-no">● 未設定</span>`;
  $("view").innerHTML = `
    <div class="view-head"><h2>系統設置</h2></div>
    <div class="set-grid">
      <div class="set-card"><h3>資料庫（驗證目標）</h3>
        ${kv("DB", esc(d.db_name))}${kv("Host", esc(d.db_host))}${kv("Port", d.db_port)}</div>
      <div class="set-card"><h3>目標 API</h3>
        ${kv("API Base", esc(d.api_base))}${kv("Platform", esc(d.platform))}</div>
      <div class="set-card"><h3>DeepSeek（用例分解器）</h3>
        ${kv("Model", esc(d.deepseek_model))}${kv("Base URL", esc(d.deepseek_base_url))}${kv("API Key", dot)}</div>
      <div class="set-card"><h3>資料量</h3>
        ${kv("工作 s_jobs", d.counts.jobs.toLocaleString())}
        ${kv("承攬制任務", d.counts.contract_tasks.toLocaleString())}
        ${kv("打工夥伴", d.counts.labors.toLocaleString())}
        ${kv("商家", d.counts.employers.toLocaleString())}
        ${kv("店鋪", d.counts.shops.toLocaleString())}</div>
    </div>`;
}

// ── 路由 ──────────────────────────────────────────────────────────────────
function route() {
  closeDrawer();
  const key = (location.hash.replace("#", "") || "jobs");
  NAV.forEach((n) => { const b = document.querySelector(`.nav button[data-k="${n.key}"]`); if (b) b.classList.toggle("active", n.key === key); });
  if (BOARDS[key]) return renderBoard(key);
  if (TABLES[key]) return renderTable(key);
  if (key === "settings") return renderSettings();
  location.hash = "jobs";
}

function init() {
  $("nav").innerHTML = NAV.map((n) => `<button data-k="${n.key}">${n.label}</button>`).join("");
  $("nav").querySelectorAll("button").forEach((b) => b.onclick = () => { location.hash = b.dataset.k; });
  $("refresh-btn").onclick = route;
  $("drawer-close").onclick = closeDrawer;
  $("drawer-mask").onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  window.addEventListener("hashchange", route);
  const tick = () => { const d = new Date(); $("clock").textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  tick(); setInterval(tick, 1000);
  api("/api/settings").then((d) => $("db-sub").textContent = `${d.db_name} @ ${d.db_host}`).catch(() => {});
  route();
}
init();
