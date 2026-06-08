"use strict";
// 系統設置：DB / API / DeepSeek 唯讀概覽 + 後台管理員帳密（可編輯持久化）。

import { $, api, apiPost, esc, toast } from "./util.js";

export async function renderSettings() {
  const d = await api("/api/settings").catch((e) => (toast(e.message), null));
  if (!d) return;
  const kv = (k, v) => `<div class="kv"><span class="key">${k}</span><span class="val">${v}</span></div>`;
  const dot = d.deepseek_key_set ? `<span class="dot-ok">● 已設定</span>` : `<span class="dot-no">● 未設定</span>`;
  const b = d.backend || {};
  const pwPh = b.password_set ? "已設定（留空＝不修改）" : "未設定";
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

      <div class="set-card set-card-wide"><h3>後台管理員（審核打工夥伴 / 店鋪）</h3>
        <p class="set-hint">用於登入後台審核打工夥伴資料與店鋪資料。密碼僅存於看板資料庫、不外洩明文。</p>
        <div class="set-form">
          <label>後台 URL<input id="be-base" type="text" value="${esc(b.base || "")}" placeholder="http://backend.chaojun.worky.com.tw" /></label>
          <label>帳號<input id="be-user" type="text" value="${esc(b.username || "")}" placeholder="管理員帳號" autocomplete="off" /></label>
          <label>密碼<input id="be-pass" type="password" value="" placeholder="${pwPh}" autocomplete="new-password" /></label>
        </div>
        <div class="set-actions">
          <button class="btn primary" id="be-save">儲存</button>
          <button class="btn ghost" id="be-test">測試登入</button>
          <span class="set-status" id="be-status"></span>
        </div>
      </div>
    </div>`;

  const status = (msg, ok) => {
    const el = $("be-status"); if (!el) return;
    el.textContent = msg; el.className = "set-status " + (ok ? "ok" : "no");
  };

  $("be-save").onclick = async () => {
    const payload = { base: $("be-base").value, username: $("be-user").value };
    const pw = $("be-pass").value;
    if (pw) payload.password = pw;          // 留空＝不修改既有密碼
    try {
      await apiPost("/api/settings/backend", payload);
      $("be-pass").value = "";
      toast("已儲存後台設定");
      renderSettings();                     // 重繪刷新 password_set 狀態
    } catch (e) { toast(e.message); }
  };

  $("be-test").onclick = async () => {
    status("登入中…", true);
    try {
      const r = await apiPost("/api/backend/login-test", {});
      status(r.message || (r.ok ? "登入成功" : "登入失敗"), !!r.ok);
    } catch (e) { status(e.message, false); }
  };
}
