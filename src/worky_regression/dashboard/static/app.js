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
  // 雜湊格式：
  //   #view 或 #view/<sub>
  //   cases 用 #cases/<tab>/<父id>/<父id2>…：第一段 sub 為領域 tab，其後各段為下鑽父用例鏈，
  //   讓「查看子任務」的層級寫進 URL（刷新 / 前進後退皆可還原）。
  let [key, ...rest] = (location.hash.replace("#", "") || "jobs").split("/");
  // 相容舊雜湊：兩個用例入口已合併為單一 cases，正規化避免白屏
  if (key === "job-cases" || key === "task-cases") key = "cases";
  NAV.forEach((n) => { const b = document.querySelector(`.nav button[data-k="${n.key}"]`); if (b) b.classList.toggle("active", n.key === key); });
  if (BOARDS[key]) return renderBoard(key);
  // rest[0] = tab，rest.slice(1) = 下鑽父用例鏈
  if (CASES[key]) return renderCases(key, rest[0], rest.slice(1));
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
  // 頂部標識改顯示「驗證目標 API」（#4：被測 DB 不再是驗證目標）
  api("/api/settings").then((d) => $("db-sub").textContent = d.api_base || d.platform || "—").catch(() => {});
  route();
}
init();
