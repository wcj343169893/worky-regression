"use strict";
// 應用骨架：頂部選單、雜湊路由、整體初始化。各業務模塊各自一檔。

import { $, api, pad } from "./util.js";
import { closeDrawer, closeModal } from "./widgets.js";
import { BOARDS, renderBoard } from "./boards.js";
import { TABLES, renderTable } from "./tables.js";
import { CASES, renderCases } from "./cases.js";
import { renderSettings } from "./settings.js";

// ── 頂部主菜單（key 對應雜湊路由 + 各業務模塊）──────────────────────────────
const NAV = [
  { key: "jobs", label: "工作看板" },
  { key: "tasks", label: "任務看板" },
  { key: "cases", label: "測試用例" },
  { key: "labors", label: "打工夥伴管理" },
  { key: "employers", label: "商家管理" },
  { key: "shops", label: "店鋪管理" },
  { key: "settings", label: "系統設置" },
];

function route() {
  closeDrawer();
  let key = (location.hash.replace("#", "") || "jobs");
  // 相容舊雜湊：兩個用例入口已合併為單一 cases，正規化避免白屏
  if (key === "job-cases" || key === "task-cases") key = "cases";
  NAV.forEach((n) => { const b = document.querySelector(`.nav button[data-k="${n.key}"]`); if (b) b.classList.toggle("active", n.key === key); });
  if (BOARDS[key]) return renderBoard(key);
  if (CASES[key]) return renderCases(key);
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
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeModal(); closeDrawer(); } });
  window.addEventListener("hashchange", route);
  const tick = () => { const d = new Date(); $("clock").textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  tick(); setInterval(tick, 1000);
  api("/api/settings").then((d) => $("db-sub").textContent = `${d.db_name} @ ${d.db_host}`).catch(() => {});
  route();
}
init();
