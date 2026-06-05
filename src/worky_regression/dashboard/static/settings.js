"use strict";
// 系統設置：DB / API / DeepSeek 設定與資料量概覽（唯讀）。

import { $, api, esc, toast } from "./util.js";

export async function renderSettings() {
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
        ${kv("API Base", esc(d.api_base))}${kv("Activity API", esc(d.activity_api_base || "—"))}${kv("Platform", esc(d.platform))}</div>
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
