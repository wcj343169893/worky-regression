"use strict";
// 頁面標記（mark up）：右上按鈕開標記模式 → 點選頁面元素 → 彈窗填內容 →
// 連同 CSS 選擇器 / 元素文字 / 路由 / 座標 / 整頁截圖存到後端，交 headless Claude worker 處理。

import { $, api, apiPost, esc, toast, fmtTs } from "./util.js";
import { openModal, closeModal } from "./widgets.js";

let active = false;          // 是否在標記模式
let hoverBox = null;         // 跟隨游標的高亮框
let lastTarget = null;       // 目前 hover 的元素

// ── html2canvas 懶載入（UMD，非 ES module；首次截圖時注入 <script>）──────────
let _h2cReady = null;
function loadHtml2Canvas() {
  if (window.html2canvas) return Promise.resolve(window.html2canvas);
  if (_h2cReady) return _h2cReady;
  _h2cReady = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "/static/vendor/html2canvas.min.js";
    s.onload = () => resolve(window.html2canvas);
    s.onerror = () => reject(new Error("html2canvas 載入失敗"));
    document.head.appendChild(s);
  });
  return _h2cReady;
}

// ── CSS 選擇器：從目標往上走到帶 id 的祖先或 body，組可定位的路徑 ───────────
function cssPath(el) {
  if (!(el instanceof Element)) return "";
  const parts = [];
  let node = el;
  while (node && node.nodeType === 1 && node !== document.body) {
    if (node.id) { parts.unshift(`#${CSS.escape(node.id)}`); break; }
    let sel = node.nodeName.toLowerCase();
    const cls = (node.className && typeof node.className === "string")
      ? node.className.trim().split(/\s+/).filter((c) => c && !c.startsWith("markup-")).slice(0, 2)
      : [];
    if (cls.length) sel += "." + cls.map((c) => CSS.escape(c)).join(".");
    // 同類兄弟加 nth-of-type 以唯一定位
    const parent = node.parentNode;
    if (parent) {
      const sames = Array.from(parent.children).filter((c) => c.nodeName === node.nodeName);
      if (sames.length > 1) sel += `:nth-of-type(${sames.indexOf(node) + 1})`;
    }
    parts.unshift(sel);
    node = node.parentNode;
  }
  return parts.join(" > ");
}

// 是否為我們自己的 UI（不可被標記，避免標到工具本身）
function isOwnUi(el) {
  return !!(el.closest && (el.closest("#markup-toggle") || el.closest(".markup-ignore")
    || el.closest("#modal") || el.closest("#toast") || el === hoverBox));
}

function ensureHoverBox() {
  if (hoverBox) return hoverBox;
  hoverBox = document.createElement("div");
  hoverBox.className = "markup-hover markup-ignore";
  document.body.appendChild(hoverBox);
  return hoverBox;
}

function onMove(e) {
  const el = document.elementFromPoint(e.clientX, e.clientY);
  if (!el || isOwnUi(el)) { if (hoverBox) hoverBox.style.display = "none"; lastTarget = null; return; }
  lastTarget = el;
  const r = el.getBoundingClientRect();
  const b = ensureHoverBox();
  b.style.display = "block";
  b.style.left = `${r.left}px`; b.style.top = `${r.top}px`;
  b.style.width = `${r.width}px`; b.style.height = `${r.height}px`;
}

function onClick(e) {
  if (isOwnUi(e.target)) return;          // 點到工具自身不攔截
  e.preventDefault(); e.stopPropagation();
  const el = lastTarget || document.elementFromPoint(e.clientX, e.clientY);
  if (!el) return;
  captureAndPopup(el);
}

function onKey(e) { if (e.key === "Escape") exitMode(); }

function enterMode() {
  if (active) return;
  active = true;
  document.body.classList.add("markup-mode");
  $("markup-toggle")?.classList.add("on");
  ensureHoverBox();
  document.addEventListener("mousemove", onMove, true);
  document.addEventListener("click", onClick, true);
  document.addEventListener("keydown", onKey, true);
  toast("標記模式：點選要標記的元素（Esc 退出）");
}

function exitMode() {
  if (!active) return;
  active = false;
  document.body.classList.remove("markup-mode");
  $("markup-toggle")?.classList.remove("on");
  if (hoverBox) hoverBox.style.display = "none";
  document.removeEventListener("mousemove", onMove, true);
  document.removeEventListener("click", onClick, true);
  document.removeEventListener("keydown", onKey, true);
}

export function toggleMarkupMode() { active ? exitMode() : enterMode(); }

// ── 點選後：算定位資訊 + 截圖 + 開彈窗 ───────────────────────────────────────
async function captureAndPopup(el) {
  const r = el.getBoundingClientRect();
  const route = (location.hash.replace("#", "") || "jobs");
  const info = {
    route,
    selector: cssPath(el),
    element_text: (el.innerText || el.textContent || "").trim().slice(0, 400),
    rect: {
      x: Math.round(r.left + window.scrollX), y: Math.round(r.top + window.scrollY),
      w: Math.round(r.width), h: Math.round(r.height),
      vw: window.innerWidth, vh: window.innerHeight,
      scrollX: Math.round(window.scrollX), scrollY: Math.round(window.scrollY),
    },
  };
  // 暫退標記模式，避免高亮框入鏡 / 彈窗誤觸
  exitMode();
  let shot = null;
  try {
    const h2c = await loadHtml2Canvas();
    const canvas = await h2c(document.body, {
      logging: false, useCORS: true, backgroundColor: "#0b1020", scale: 0.7,
      x: window.scrollX, y: window.scrollY, width: window.innerWidth, height: window.innerHeight,
      ignoreElements: (node) => node.classList && node.classList.contains("markup-ignore"),
    });
    shot = canvas.toDataURL("image/png");
  } catch (err) {
    console.warn("截圖失敗，僅存定位資訊：", err);
  }
  openMarkupForm(info, shot);
}

function openMarkupForm(info, shot) {
  const preview = shot
    ? `<img class="markup-shot-preview" src="${shot}" alt="截圖預覽" />`
    : `<div class="sub2">（截圖未取得，仍會存定位資訊）</div>`;
  openModal(`
    <h3 class="modal-title">新增頁面標記</h3>
    <div class="markup-form">
      <div class="markup-meta">
        <div><span class="k">路由</span> <code>#${esc(info.route)}</code></div>
        <div><span class="k">元素</span> <code class="markup-sel">${esc(info.selector || "-")}</code></div>
        ${info.element_text ? `<div><span class="k">文字</span> <span class="markup-eltext">${esc(info.element_text.slice(0, 120))}</span></div>` : ""}
      </div>
      ${preview}
      <textarea id="markup-content" rows="4" placeholder="描述要在這個元素 / 區塊做什麼改動或回饋…"></textarea>
      <div class="markup-actions">
        <button class="btn" id="markup-cancel">取消</button>
        <button class="btn primary" id="markup-submit">送出標記</button>
      </div>
    </div>`);
  const ta = $("markup-content");
  ta?.focus();
  $("markup-cancel").onclick = closeModal;
  $("markup-submit").onclick = async () => {
    const content = (ta.value || "").trim();
    if (!content) { toast("請先填寫標記內容"); ta.focus(); return; }
    const btn = $("markup-submit"); btn.disabled = true; btn.textContent = "送出中…";
    try {
      await apiPost("/api/markups", { ...info, content, screenshot: shot });
      closeModal();
      invalidateMarkupCache(); scheduleOverlayRefresh();   // 新標記立刻在頁面上框出來
      toast("標記已送出，交由 Claude 處理");
    } catch (err) {
      toast("送出失敗：" + err.message);
      btn.disabled = false; btn.textContent = "送出標記";
    }
  };
  // 送出後可在「標記」頁查看處理狀態
}

// ── 標記管理頁（nav: 標記）──────────────────────────────────────────────────
const ST_LABEL = { pending: "待處理", processing: "處理中", done: "已完成", failed: "失敗" };
const KIND_LABEL = { feedback: "意見反饋", global: "全局指令" };   // page（預設）不顯示標籤
const fmtMs = (ms) => !ms ? "" : ms >= 60000 ? `${Math.round(ms / 60000)}m${Math.round((ms % 60000) / 1000)}s`
  : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
// 狀態篩選 tab（"" = 全部）+ 分頁。管理頁是「分頁查詢」：和 overlay/輪詢用的全量抓取分開，
// 後者仍需所有標記才能在各頁畫框；前者只取當前頁切片，附 status / q 條件。
const MARKUP_STATUSES = [["", "全部"], ["pending", "待處理"], ["processing", "處理中"], ["done", "已完成"], ["failed", "失敗"]];
const MARKUP_PAGE_SIZE = 20;
let mq = { status: "", q: "", page: 0 };   // 管理頁查詢狀態（跨重渲染保留）
let mqTotal = 0;                            // 最近一次查詢的符合總筆數（算頁數用）

function markupCardHtml(m) {
  const resolved = !!m.resolved;
  // 回滾：worker 有記錄改檔（或已解決時提交了 commit）且尚未回滾過，才可撤銷
  const canRollback = !m.rolled_back && ((m.files_changed && m.files_changed.length) || m.commit_sha);
  return `
      <div class="markup-card${resolved ? " is-resolved" : ""}" data-id="${m.id}">
        <div class="markup-card-shot">${m.screenshot_path
          ? `<img loading="lazy" src="/api/markups/${m.id}/screenshot" alt="截圖" />`
          : `<div class="markup-noshot">無截圖</div>`}</div>
        <div class="markup-card-body">
          <div class="markup-card-top">
            <span class="badge b-${m.status}">${ST_LABEL[m.status] || m.status}</span>
            ${KIND_LABEL[m.kind] ? `<span class="pill">${KIND_LABEL[m.kind]}</span>` : ""}
            <code>#${esc(m.route)}</code>
            <span class="sub2">${fmtTs(m.created_at)}</span>
            ${m.ip ? `<span class="sub2">IP ${esc(m.ip)}</span>` : ""}
            ${m.elapsed_ms ? `<span class="sub2">耗時 ${fmtMs(m.elapsed_ms)}</span>` : ""}
            ${resolved ? `<span class="pill pill-ok">已解決（源頁不顯示）</span>` : ""}
            ${m.rolled_back ? `<span class="pill">已回滾</span>` : ""}
            ${m.commit_sha ? `<code class="sub2" title="已解決時提交的 commit">${esc(m.commit_sha.slice(0, 10))}</code>` : ""}
          </div>
          <div class="markup-card-content">${esc(m.content)}</div>
          <code class="markup-sel">${esc(m.selector || "-")}</code>
          ${(m.files_changed && m.files_changed.length) ? `<details class="markup-result"><summary>改動檔案（${m.files_changed.length}）</summary><pre>${esc(m.files_changed.join("\n"))}</pre></details>` : ""}
          ${m.result ? `<details class="markup-result"><summary>處理結果</summary><pre>${esc(m.result)}</pre></details>` : ""}
          ${(m.replies && m.replies.length) ? `<div class="markup-replies">${m.replies.map((rp) =>
            `<div class="markup-reply"><span class="markup-reply-k">↳ 回覆</span> <span class="sub2">${fmtTs(rp.at)}</span>
              <div class="markup-reply-text">${esc(rp.text || "")}</div></div>`).join("")}</div>` : ""}
          <div class="markup-reply-box">
            <textarea class="markup-reply-input" data-id="${m.id}" rows="2"
              placeholder="對處理結果不滿意？補充說明，再次優化這個問題…"></textarea>
            <button class="btn markup-reply-send" data-id="${m.id}">送出回覆並重新優化</button>
          </div>
          <div class="markup-card-act">
            <button class="btn ${resolved ? "ok" : ""} markup-resolve" data-id="${m.id}" data-resolved="${resolved ? 1 : 0}">${resolved ? "取消解決" : "已解決"}</button>
            ${canRollback ? `<button class="btn ghost markup-rollback" data-id="${m.id}" title="撤銷此標記的代碼修改（已提交→revert；未提交→還原檔案）">⤺ 回滾</button>` : ""}
            <button class="btn ghost markup-del" data-id="${m.id}">刪除</button>
          </div>
        </div>
      </div>`;
}

function wireMarkupListHandlers(box) {
  box.querySelectorAll(".markup-reply-send").forEach((b) => b.onclick = async () => {
    const ta = box.querySelector(`.markup-reply-input[data-id="${b.dataset.id}"]`);
    const content = (ta?.value || "").trim();
    if (!content) { toast("請先填寫回覆內容"); ta?.focus(); return; }
    b.disabled = true; b.textContent = "送出中…";
    try {
      await apiPost("/api/markups/reply", { id: Number(b.dataset.id), content });
      invalidateMarkupCache();   // 狀態打回 pending → 頁面虛線框會重新變黃
      toast("已重新排入處理（需 worker 運行）");
      pollSoon();                // 立刻拉一次，徽章/框即時轉回待處理
      loadMarkupPage();          // 重抓當前頁（保留所在 tab / 分頁）
    } catch (e) {
      toast("送出失敗：" + e.message);
      b.disabled = false; b.textContent = "送出回覆並重新優化";
    }
  });
  // 已解決開關：切換後源頁面框即時隱藏/恢復；標為解決時後端會把該標記動到的檔案
  // 提交成獨立 commit（回應帶 commit_sha / commit_warning），供之後回滾 revert。
  box.querySelectorAll(".markup-resolve").forEach((b) => b.onclick = async () => {
    const want = b.dataset.resolved !== "1";   // 目前未解決 → 設為已解決；反之取消
    b.disabled = true;
    try {
      const r = await apiPost("/api/markups/resolve", { id: Number(b.dataset.id), resolved: want });
      invalidateMarkupCache();
      scheduleOverlayRefresh();
      if (want && r.commit_sha) toast(`已解決，代碼已提交 ${r.commit_sha.slice(0, 10)}`);
      else if (want && r.commit_warning) toast(`已解決（${r.commit_warning}）`);
      else toast(want ? "已標為解決，源頁面不再顯示" : "已取消解決，源頁面恢復顯示");
      loadMarkupPage({ preserveDrafts: true });
    } catch (e) { toast("操作失敗：" + e.message); b.disabled = false; }
  });
  // 回滾：撤銷該標記的代碼修改（已提交→git revert；未提交→還原工作區檔案）
  box.querySelectorAll(".markup-rollback").forEach((b) => b.onclick = async () => {
    if (!confirm("確定撤銷此標記的代碼修改？\n已提交者會產生一筆 revert commit；未提交者直接還原檔案。")) return;
    b.disabled = true; b.textContent = "回滾中…";
    try {
      const r = await apiPost("/api/markups/rollback", { id: Number(b.dataset.id) });
      toast("回滾完成：" + (r.note || ""));
      invalidateMarkupCache(); pollSoon(); loadMarkupPage();
    } catch (e) { toast("回滾失敗：" + e.message); b.disabled = false; b.textContent = "⤺ 回滾"; }
  });
  box.querySelectorAll(".markup-del").forEach((b) => b.onclick = async () => {
    try { await apiPost("/api/markups/delete", { id: Number(b.dataset.id) }); invalidateMarkupCache(); pollSoon(); loadMarkupPage(); }
    catch (e) { toast("刪除失敗：" + e.message); }
  });
  box.querySelectorAll(".markup-card-shot img").forEach((img) => img.onclick = () =>
    openModal(`<img class="markup-shot-preview" style="max-height:80vh" src="${img.src}" alt="截圖" />`));
}

// 重新填標記清單；preserveDrafts=true 時保留使用者正在打字的回覆內容與焦點（輪詢就地更新用）。
function fillMarkupList(box, items, { preserveDrafts = false } = {}) {
  if (!items.length) { box.innerHTML = `<div class="empty">尚無標記</div>`; return; }
  let drafts = {}, focusedId = null;
  if (preserveDrafts) {
    box.querySelectorAll(".markup-reply-input").forEach((ta) => {
      if (ta.value) drafts[ta.dataset.id] = ta.value;
      if (ta === document.activeElement) focusedId = ta.dataset.id;
    });
  }
  box.innerHTML = items.map(markupCardHtml).join("");
  wireMarkupListHandlers(box);
  for (const [id, val] of Object.entries(drafts)) {
    const ta = box.querySelector(`.markup-reply-input[data-id="${id}"]`);
    if (ta) ta.value = val;
  }
  if (focusedId) box.querySelector(`.markup-reply-input[data-id="${focusedId}"]`)?.focus();
}

// 依 mq（status/q/page）抓當前頁切片並填入清單 + 更新分頁器。
// preserveDrafts：輪詢就地更新時保留使用者正在打的回覆草稿與焦點。
async function loadMarkupPage({ preserveDrafts = false } = {}) {
  const box = $("markup-list");
  if (!box) return;
  const params = new URLSearchParams({
    limit: String(MARKUP_PAGE_SIZE), offset: String(mq.page * MARKUP_PAGE_SIZE),
  });
  if (mq.status) params.set("status", mq.status);
  if (mq.q) params.set("q", mq.q);
  try {
    const d = await api(`/api/markups?${params.toString()}`);
    mqTotal = d.total || 0;
    // 當前頁可能因刪除/篩選而越界（如刪到本頁最後一筆）→ 回退一頁重抓
    const pages = Math.max(1, Math.ceil(mqTotal / MARKUP_PAGE_SIZE));
    if (mq.page > pages - 1 && mq.page > 0) { mq.page = pages - 1; return loadMarkupPage({ preserveDrafts }); }
    fillMarkupList(box, d.items || [], { preserveDrafts });
    updateMarkupPager(pages);
  } catch (e) {
    box.innerHTML = `<div class="empty">載入失敗：${esc(e.message)}</div>`;
  }
}

function updateMarkupPager(pages) {
  const info = $("mq-info");
  if (info) info.innerHTML = `第 <b>${mq.page + 1}</b> / ${pages} 頁 · 共 ${mqTotal} 筆`;
  const atFirst = mq.page <= 0, atLast = mq.page >= pages - 1;
  const dis = (id, v) => { const b = $(id); if (b) b.disabled = v; };
  dis("mq-first", atFirst); dis("mq-prev", atFirst);
  dis("mq-next", atLast); dis("mq-last", atLast);
}

// 狀態 tab 對應的合法值（"" = 全部，對應乾淨的 #markups）。
const MARKUP_STATUS_KEYS = ["pending", "processing", "done", "failed"];

// 點狀態 tab：寫入雜湊（#markups/<status>，全部用乾淨的 #markups）讓網址同步、可刷新還原。
function selectMarkupStatus(status) {
  const newHash = status ? `markups/${status}` : "markups";
  if (location.hash.replace("#", "") === newHash) {
    if (mq.status !== status) { mq.status = status; mq.page = 0; }
    renderMarkups(status);
  } else {
    location.hash = newHash;   // hashchange → route → renderMarkups 自動套用
  }
}

export async function renderMarkups(tabKey) {
  // 由雜湊（#markups/<status>）定位狀態 tab；缺省 / 不合法 → 全部("")
  const status = MARKUP_STATUS_KEYS.includes(tabKey) ? tabKey : "";
  if (status !== mq.status) { mq.status = status; mq.page = 0; }
  const view = $("view");
  const tabs = MARKUP_STATUSES.map(([k, l]) =>
    `<button class="dc-tab${k === mq.status ? " active" : ""}" data-st="${k}">${l}</button>`).join("");
  view.innerHTML = `<section class="cases-page">
    <div class="view-head"><h2>頁面標記</h2>
      <span class="sub2">點右上「✎ 標記」在任一頁標註元素；headless Claude worker 會輪詢處理（狀態即時更新）。</span></div>
    <div class="dc-tabs">${tabs}</div>
    <div class="card cases-list">
      <div class="panel-head"><h3>標記清單</h3>
        <div class="mq-tools">
          <input type="search" id="mq-q" placeholder="搜尋 內容 / 路由 / 選擇器…" value="${esc(mq.q)}" />
          <button class="btn primary" id="mq-new" title="不綁定頁面元素，給系統全局添加修改指令">＋ 新增</button>
        </div></div>
      <div id="markup-list" class="markup-list"><div class="empty">載入中…</div></div>
      <div class="pager">
        <button class="btn ghost" id="mq-first">« 首頁</button>
        <button class="btn ghost" id="mq-prev">‹ 上一頁</button>
        <span id="mq-info"></span>
        <button class="btn ghost" id="mq-next">下一頁 ›</button>
        <button class="btn ghost" id="mq-last">尾頁 »</button>
      </div>
    </div>
  </section>`;
  // 狀態 tab：點擊寫入雜湊（同步網址、可刷新還原）
  view.querySelectorAll(".dc-tab[data-st]").forEach((b) =>
    b.onclick = () => selectMarkupStatus(b.dataset.st));
  // 「＋新增」：給系統全局添加修改指令（kind=global，無頁面定位，worker 對整倉生效）
  $("mq-new").onclick = () => {
    openModal(`
      <h3 class="modal-title">新增全局修改指令</h3>
      <div class="markup-form">
        <p class="sub2">不綁定頁面元素，直接對看板 / 框架整體下修改指令，交後台 worker 處理。</p>
        <textarea id="mq-new-text" rows="5" placeholder="描述要對系統做的修改，例：所有列表頁的分頁器加上「跳到第 N 頁」輸入框…"></textarea>
        <div class="markup-actions">
          <button class="btn" id="mq-new-cancel">取消</button>
          <button class="btn primary" id="mq-new-send">發送</button>
        </div>
      </div>`);
    const ta = $("mq-new-text"); ta?.focus();
    $("mq-new-cancel").onclick = closeModal;
    $("mq-new-send").onclick = async () => {
      const content = (ta.value || "").trim();
      if (!content) { toast("請先填寫指令內容"); ta.focus(); return; }
      const btn = $("mq-new-send"); btn.disabled = true; btn.textContent = "發送中…";
      try {
        await apiPost("/api/markups", { kind: "global", route: "markups", content });
        closeModal();
        invalidateMarkupCache(); pollSoon(); loadMarkupPage();
        toast("全局指令已送出，交由 worker 處理");
      } catch (e) { toast("發送失敗：" + e.message); btn.disabled = false; btn.textContent = "發送"; }
    };
  };
  // 搜尋（debounce 300ms）：回第一頁重抓
  let t; $("mq-q").oninput = (e) => {
    clearTimeout(t); mq.q = e.target.value;
    t = setTimeout(() => { mq.page = 0; loadMarkupPage(); }, 300);
  };
  // 翻頁
  const pagesNow = () => Math.max(1, Math.ceil(mqTotal / MARKUP_PAGE_SIZE));
  $("mq-first").onclick = () => { if (mq.page > 0) { mq.page = 0; loadMarkupPage(); } };
  $("mq-prev").onclick = () => { if (mq.page > 0) { mq.page--; loadMarkupPage(); } };
  $("mq-next").onclick = () => { if (mq.page < pagesNow() - 1) { mq.page++; loadMarkupPage(); } };
  $("mq-last").onclick = () => { const p = pagesNow() - 1; if (mq.page < p) { mq.page = p; loadMarkupPage(); } };
  loadMarkupPage();
}

// ── 既有標記在頁面上的可視化（不同顏色虛線框 + 右上角圖標）───────────────────
// 進到某頁時，把該路由上「已建立的標記」用虛線框標出（不影響底下元素操作）：
//   已解決(done)=綠、未解決(pending/processing)=黃、失敗(failed)=紅。
// 定位優先用 selector 即時查 DOM（對版面變動較穩），查不到才退回存檔的文件座標 rect。
// 虛線框配色與「標記狀態徽章」一致：pending=黃 processing=青 done=綠 failed=紅
const OVERLAY_CLS = { done: "done", failed: "failed", pending: "pending", processing: "processing" };
let overlayLayer = null;     // body 下、position:fixed 的圖層（視口座標，跟著重新定位）
let activeOverlays = [];     // [{box, target, fallback}]：目前畫出的框 + 其定位來源
let markupCache = null;      // /api/markups 快取（換頁只重濾、增刪才失效）
let overlayTimer = null;
let repositionRaf = 0;
// ── 狀態即時更新（輪詢）──
// worker 是獨立進程直接寫 DB、server 無事件鉤子，故前端自適應輪詢：有 in-flight 時快、閒置慢、
// 分頁隱藏時暫緩。狀態有變才動 DOM（重畫框 + 就地更新管理頁徽章/結果）。
let pollTimer = 0;
let lastSig = "";

function curRoute() { return location.hash.replace("#", "") || "jobs"; }

function ensureOverlayLayer() {
  if (overlayLayer) return overlayLayer;
  overlayLayer = document.createElement("div");
  overlayLayer.id = "markup-overlays";
  overlayLayer.className = "markup-ignore";   // 不被標記模式攔截、也不入截圖
  document.body.appendChild(overlayLayer);
  return overlayLayer;
}

function loadMarkupsForOverlay(force = false) {
  if (force || !markupCache) markupCache = api("/api/markups?limit=200").then((d) => d.items || []).catch(() => []);
  return markupCache;
}
function invalidateMarkupCache() { markupCache = null; }

// 狀態指紋：id+狀態+有無結果+回覆數；任一變動才觸發重繪。
function statusSig(items) {
  return items.map((m) => `${m.id}:${m.status}:${m.resolved ? 1 : 0}:${m.result ? 1 : 0}:${(m.replies || []).length}`).join("|");
}
function hasInFlight(items) {
  return items.some((m) => m.status === "pending" || m.status === "processing");
}

async function pollMarkups() {
  pollTimer = 0;
  let items = null;
  if (!document.hidden) {
    try { items = (await api("/api/markups?limit=200")).items || []; } catch (_) { items = null; }
  }
  if (items) {
    markupCache = Promise.resolve(items);          // 餵給 overlay / renderMarkups 共用
    const sig = statusSig(items);
    if (sig !== lastSig) {                          // 狀態有變 → 重畫框 + 就地更新管理頁
      lastSig = sig;
      refreshOverlays();
      // 管理頁就地更新：重抓「當前頁切片」（帶 status/q 篩選），而非整批 200 筆，保留回覆草稿。
      if (curRoute().split("/")[0] === "markups") loadMarkupPage({ preserveDrafts: true });
    }
  }
  // 有待處理/處理中 → 3s 緊盯；否則 15s 慢輪詢（仍能接住外部新增/回覆）；隱藏分頁拉長到 30s。
  const next = document.hidden ? 30000 : (items && hasInFlight(items) ? 3000 : 15000);
  pollTimer = setTimeout(pollMarkups, next);
}

// 立即排一次輪詢（送出回覆/刪除等本地操作後，讓狀態馬上對齊）。
function pollSoon() { clearTimeout(pollTimer); pollTimer = setTimeout(pollMarkups, 200); }

// #view(=.content) 才是真正的滾動容器（window 不滾）；框依視口座標定位、捲動/縮放時重算。
function positionOverlays() {
  const view = $("view");
  const vr = view ? view.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
  for (const o of activeOverlays) {
    let r = null;
    if (o.target && o.target.isConnected) {
      const b = o.target.getBoundingClientRect();
      if (b.width || b.height) r = { left: b.left, top: b.top, width: b.width, height: b.height };
    }
    if (!r && o.fallback) {  // selector 失效時退回存檔 rect（捕捉時 window/捲動為基準，最佳努力）
      const f = o.fallback;
      r = { left: f.x - view.scrollLeft, top: f.y - view.scrollTop, width: f.w, height: f.h };
    }
    // 捲出 content 可視範圍就藏起來，避免框蓋到頂部選單
    if (!r || r.top > vr.bottom || r.top + r.height < vr.top) { o.box.style.display = "none"; continue; }
    o.box.style.display = "block";
    o.box.style.left = `${r.left}px`; o.box.style.top = `${r.top}px`;
    o.box.style.width = `${r.width}px`; o.box.style.height = `${r.height}px`;
  }
}

function scheduleReposition() {
  if (repositionRaf) return;
  repositionRaf = requestAnimationFrame(() => { repositionRaf = 0; positionOverlays(); });
}

async function refreshOverlays() {
  const layer = ensureOverlayLayer();
  const route = curRoute();
  if (route.split("/")[0] === "markups") { layer.innerHTML = ""; activeOverlays = []; return; }  // 清單頁不畫框
  const items = await loadMarkupsForOverlay();
  if (curRoute() !== route) return;     // 非同步回來時已換頁 → 放棄這批
  layer.innerHTML = "";
  activeOverlays = [];
  for (const m of items) {
    if (m.route !== route) continue;
    if (m.resolved) continue;                  // 已解決 → 源頁面不畫框（取消解決後又會恢復）
    let target = null;
    if (m.selector) { try { target = document.querySelector(m.selector); } catch (_) { target = null; } }
    const fallback = (m.rect && typeof m.rect.x === "number") ? m.rect : null;
    if (!target && !fallback) continue;
    const cls = OVERLAY_CLS[m.status] || "pending";
    const box = document.createElement("div");
    box.className = `markup-overlay mo-${cls}`;
    const icon = document.createElement("button");
    icon.type = "button";
    icon.className = `markup-overlay-icon mo-${cls}`;
    icon.textContent = "✎";
    icon.title = `[${ST_LABEL[m.status] || m.status}] ${m.content || ""}`.slice(0, 200);
    icon.onclick = (e) => { e.preventDefault(); e.stopPropagation(); openMarkupDetail(m); };
    box.appendChild(icon);
    layer.appendChild(box);
    activeOverlays.push({ box, target, fallback });
  }
  positionOverlays();
}

function openMarkupDetail(m) {
  openModal(`
    <h3 class="modal-title">頁面標記 #${m.id}</h3>
    <div class="markup-form">
      <div class="markup-card-top">
        <span class="badge b-${m.status}">${ST_LABEL[m.status] || m.status}</span>
        <code>#${esc(m.route)}</code>
        <span class="sub2">${fmtTs(m.created_at)}</span>
        ${m.ip ? `<span class="sub2">IP ${esc(m.ip)}</span>` : ""}
        ${m.elapsed_ms ? `<span class="sub2">耗時 ${fmtMs(m.elapsed_ms)}</span>` : ""}
      </div>
      <div class="markup-card-content">${esc(m.content)}</div>
      <code class="markup-sel">${esc(m.selector || "-")}</code>
      ${m.screenshot_path ? `<img class="markup-shot-preview" src="/api/markups/${m.id}/screenshot" alt="截圖" />` : ""}
      ${m.result ? `<details class="markup-result" open><summary>處理結果</summary><pre>${esc(m.result)}</pre></details>` : ""}
      <div class="markup-actions">
        <button class="btn ok" id="mo-detail-resolve">已解決</button>
        <button class="btn" id="mo-detail-close">關閉</button>
      </div>
    </div>`);
  $("mo-detail-close").onclick = closeModal;
  // 已解決：隱藏源頁面的框 + 後端提交該標記的代碼修改（回應帶 commit 信息）
  $("mo-detail-resolve").onclick = async () => {
    const btn = $("mo-detail-resolve"); btn.disabled = true; btn.textContent = "處理中…";
    try {
      const r = await apiPost("/api/markups/resolve", { id: m.id, resolved: true });
      closeModal();
      invalidateMarkupCache(); scheduleOverlayRefresh(); pollSoon();
      if (r.commit_sha) toast(`已解決，代碼已提交 ${r.commit_sha.slice(0, 10)}`);
      else if (r.commit_warning) toast(`已解決（${r.commit_warning}）`);
      else toast("已標為解決，源頁面不再顯示");
    } catch (e) { toast("操作失敗：" + e.message); btn.disabled = false; btn.textContent = "已解決"; }
  };
}

function scheduleOverlayRefresh() {
  clearTimeout(overlayTimer);
  overlayTimer = setTimeout(() => { refreshOverlays(); }, 150);
}

// app 啟動時呼叫一次：
//   · #view 內容變動 / 換頁 → 重建框（重新查 selector）
//   · #view 捲動 / 視窗縮放 → 只重算現有框位置（rAF 節流，省成本）
export function initMarkupOverlays() {
  ensureOverlayLayer();
  const view = $("view");
  if (view) {
    new MutationObserver(scheduleOverlayRefresh).observe(view, { childList: true, subtree: true });
    view.addEventListener("scroll", scheduleReposition, { passive: true });
  }
  window.addEventListener("hashchange", scheduleOverlayRefresh);
  window.addEventListener("resize", scheduleReposition);
  // 分頁從背景切回前景 → 立刻對齊一次狀態
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollSoon(); });
  scheduleOverlayRefresh();
  pollMarkups();   // 啟動狀態即時輪詢
}
