// 自动审查配置的独立页（设置 · 安全设置 → 「配置」）。
//
// 形状照记忆库（`MemoryLibrary.jsx`）：整块内容换页、左上角返回。
// 三段（allow / deny / environment）：
//    · 允许 —— 用户明确授权：命中就放，哪怕内置规则说要拦；
//    · 禁止 —— 用户明确禁止：命中就拦，哪怕内置规则说可以；
//    · 环境 —— 用户的环境说明（信任哪些域名/仓库、数据在哪）：它是背景，
//              不直接放行或拦截。
//   所以一条自由文本混着两种方向时，分类器只能猜；分开之后每段有自己的判据。
//
// 数据是 `settings.json` 的 `auto_review_rules`（对象，三段各一个字符串数组）。
//   写：`POST /api/settings`；读：父组件传进来的 `settings`（改完 `onReload` 刷新）。
// 只验形状（后端 `normalize_rules`）：这一层负责增 / 删 / 改，
//   不做内容审查 —— 规则写什么是用户自己的授权决定。
// 三段都管不到限位闸（出网命令族 / 受保护路径）—— 那句写在页面顶部。
import React, { useState } from "react";
import { jsonRequest } from "./api.js";
import { t } from "./i18n.js";

const SECTIONS = [
  { id: "allow", label: "允许",
    hint: "命中这里的动作即使撞上危险规则，也放行。" },
  { id: "deny", label: "禁止",
    hint: "命中这里的动作即使看起来无害，也拦下，并把出路告诉 Agent。" },
  { id: "environment", label: "环境",
    hint: "你对环境的说明（信任的域名 / 仓库、数据在哪）—— 它只是背景，不直接放行或拦截。" },
];

export function AutoRulesPanel({ client, notify, settings, onReload, onBack }) {
  const [tab, setTab] = useState("allow");
  const [draft, setDraft] = useState("");
  const [edit, setEdit] = useState(null);       // { index, value }
  const [busy, setBusy] = useState(false);

  const all = (settings && settings.auto_review_rules) || {};
  const itemsOf = (id) => (Array.isArray(all[id]) ? all[id] : []);
  const items = itemsOf(tab);

  /** 存三段（不是只存当前这一段）：后端收的是整个对象，少给一段等于把它清空。 */
  async function save(sectionId, nextItems) {
    setBusy(true);
    try {
      const payload = {};
      for (const s of SECTIONS) {
        payload[s.id] = s.id === sectionId ? nextItems : itemsOf(s.id);
      }
      const r = await client.call("/api/settings",
        jsonRequest("POST", { auto_review_rules: payload }));
      notify((r && r.message) || t("已保存。"));
      await onReload();
      return true;
    } catch (e) {
      notify(e.message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function add() {
    const v = draft.trim();
    if (!v || busy) return;
    if (await save(tab, items.concat([v]))) setDraft("");
  }

  async function remove(i) {
    if (busy) return;
    await save(tab, items.filter((_x, j) => j !== i));
  }

  async function saveEdit() {
    const v = String((edit && edit.value) || "").trim();
    if (!v || !edit || busy) return;
    const next = items.slice();
    next[edit.index] = v;
    if (await save(tab, next)) setEdit(null);
  }

  const cur = SECTIONS.find((s) => s.id === tab) || SECTIONS[0];

  return (
    <div className="mem-page auto-rules-page">
      <div className="mem-head">
        <button className="mem-back" onClick={onBack}>{t("← 返回")}</button>
        <h2>{t("自动审查配置")}</h2>
      </div>

      <div className="auto-rules-tabs">
        {SECTIONS.map((s) => (
          <button key={s.id}
                  className={"auto-rules-tab" + (s.id === tab ? " on" : "")}
                  onClick={() => { setTab(s.id); setEdit(null); setDraft(""); }}>
            {t(s.label)}
            <small>{itemsOf(s.id).length}</small>
          </button>
        ))}
      </div>
      <p className="auto-rules-hint">{t(cur.hint)}</p>
      <p className="auto-rules-note">
        {t("这些规则不会移除限位闸：出网命令族与受保护路径照旧先被拦下。")}
      </p>

      <div className="auto-rules-list">
        {items.map((s, i) => {
          const ed = edit && edit.index === i ? edit : null;
          return (
            <div className="auto-rules-item" key={s + "|" + i}>
              {ed ? (
                <>
                  <input className="set-input" value={ed.value} autoFocus
                         disabled={busy}
                         onChange={(e) => setEdit({ index: i, value: e.target.value })}
                         onKeyDown={(e) => {
                           if (e.key === "Enter") { e.preventDefault(); saveEdit(); }
                           else if (e.key === "Escape") { e.preventDefault(); setEdit(null); }
                         }} />
                  <button className="wl-del" title={t("保存这一条")}
                          disabled={busy || !String(ed.value || "").trim()}
                          onClick={saveEdit}>✓</button>
                  <button className="wl-del" title={t("取消")}
                          disabled={busy} onClick={() => setEdit(null)}>✕</button>
                </>
              ) : (
                <>
                  <span className="auto-rules-text">{s}</span>
                  <button className="wl-del" title={t("改这一条")} disabled={busy}
                          onClick={() => setEdit({ index: i, value: s })}>✎</button>
                  <button className="wl-del" title={t("删掉它")} disabled={busy}
                          onClick={() => remove(i)}>✕</button>
                </>
              )}
            </div>
          );
        })}
      </div>

      <div className="wl-add auto-rules-add">
        <input className="set-input"
               placeholder={t("写一条，回车或点「添加」")}
               value={draft} disabled={busy}
               onChange={(e) => setDraft(e.target.value)}
               onKeyDown={(e) => {
                 if (e.key === "Enter") { e.preventDefault(); add(); }
               }} />
        <button className="skill-btn" disabled={busy || !draft.trim()}
                onClick={add}>{t("＋ 添加")}</button>
      </div>
    </div>
  );
}
