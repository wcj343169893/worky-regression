"use strict";
// 系統設置：API / DeepSeek 唯讀概覽 + 後台管理員帳密（可編輯持久化）。
// #4：不再顯示/查詢被測後端 DB（移除「資料庫（驗證目標）」與即時 COUNT 的「資料量」卡）。

import { $, api, apiPost, esc, toast } from "./util.js";

export async function renderSettings() {
  const d = await api("/api/settings").catch((e) => (toast(e.message), null));
  if (!d) return;
  const kv = (k, v) => `<div class="kv"><span class="key">${k}</span><span class="val">${v}</span></div>`;
  const dbConsistencyCard = (c) => {
    if (!c) return "";
    const ok = !!c.consistent;
    const badge = ok
      ? `<span class="dot-ok">● 一致</span>`
      : `<span class="dot-no">● 不一致</span>`;
    const hint = ok
      ? `被測倉分支與 .env 庫名相符。`
      : `<b>分支與 .env 庫名不符！</b>切分支＝換一套測試數據：請改 .env 的 WORKY_DB_NAME 為「預期庫」並重建帳號池，或切回對應分支。`;
    return `<div class="set-card set-card-2x"><h3>被測庫一致性 ${badge}</h3>
      ${kv("被測倉分支", esc(c.branch || "（讀不到）"))}
      ${kv("分支推算庫", esc(c.expected_db || "—"))}
      ${kv(".env WORKY_DB_NAME", esc(c.configured_db || "—"))}
      <p class="set-hint">${hint}</p></div>`;
  };
  const dot = d.deepseek_key_set ? `<span class="dot-ok">● 已設定</span>` : `<span class="dot-no">● 未設定</span>`;
  const b = d.backend || {};
  const pwPh = b.password_set ? "已設定（留空＝不修改）" : "未設定";
  $("view").innerHTML = `
    <div class="view-head"><h2>系統設置</h2></div>
    <div class="set-grid">
      <div class="set-card"><h3>目標 API（驗證以此為準）</h3>
        ${kv("API Base", esc(d.api_base))}${kv("Activity API", esc(d.activity_api_base || "—"))}${kv("Platform", esc(d.platform))}</div>
      <div class="set-card"><h3>DeepSeek（用例分解器）</h3>
        ${kv("Model", esc(d.deepseek_model))}${kv("Base URL", esc(d.deepseek_base_url))}${kv("API Key", dot)}</div>
      <div class="set-card"><h3>QA 看板資料庫</h3>
        ${kv("DB", esc(d.qa_db_name))}<p class="set-hint">框架自身的庫（用例/執行/帳號池）；不含被測後端 DB。</p></div>
      ${dbConsistencyCard(d.db_consistency)}

      <div class="set-card"><h3>後台管理員（審核打工夥伴 / 店鋪）</h3>
        <p class="set-hint">用於登入後台審核打工夥伴資料與店鋪資料。密碼僅存於看板資料庫、不外洩明文。</p>
        <div class="set-form">
          <label>後台 URL<input id="be-base" type="text" value="${esc(b.base || "")}" placeholder="https://backend.chaojun.worky.com.tw" /></label>
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
