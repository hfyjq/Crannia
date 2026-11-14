// 设置里的名单类编辑器：访问白名单 / 规避目录 / 监听目录。
//
// 三样的形状相同（一串条目，能加能删），所以共用一个子页 —— 设置那一栏
// 只留一行摘要加一颗「设置」，点进来才是完整列表。
//
// 条目由父层拉好传进来：摘要与列表必须是同一份数据，各拉一份时两个数
// 会对不上。
//
// `running` 与 `enabled` 是两件事：「开着」不等于「在跑」。目录不存在时
// 正是"启用着、但一条都不会触发"，那种状态只画一个开着就是标签在说谎。
//
// 删除一律先问一次 —— 单条与"多选批量"走同一个确认框。
import React, { useState } from "react";
import { Confirm } from "./Confirm.jsx";
import { t } from "./i18n.js";

/** 一条条目的稳定标识：清单里允许重复（两条同名路径），所以带上下标。 */
function keyOf(item, i, isWatchers) {
  return isWatchers ? String(item.id) : String(item) + "|" + i;
}

export function ListEditor({ row, items, busy, connected, onBack,
                             onAdd, onRemove, onRemoveMany, onReset, onToggleWt }) {
  const [input, setInput] = useState("");
  const [selecting, setSelecting] = useState(false);
  const [sel, setSel] = useState(() => new Set());
  const [ask, setAsk] = useState(null);          // {keys:Set, list:[...]}
  const isWatchers = row.kind === "watchers";

  function submit() {
    const v = input.trim();
    if (!v || busy || !connected) return;
    onAdd(v);
    setInput("");
  }

  function toggleSel(k) {
    setSel((old) => {
      const next = new Set(old);
      if (next.has(k)) next.delete(k); else next.add(k);
      return next;
    });
  }

  function pickAll() {
    setSelecting((on) => {
      if (on) setSel(new Set());
      return !on;
    });
  }

  function askOne(item, i) {
    setAsk({ list: [item], keys: new Set([keyOf(item, i, isWatchers)]) });
  }

  function askMany() {
    const picked = items.filter((it, i) => sel.has(keyOf(it, i, isWatchers)));
    if (picked.length) setAsk({ list: picked, keys: new Set(sel) });
  }

  async function doDelete() {
    const picked = (ask && ask.list) || [];
    setAsk(null);
    if (!picked.length) return;
    if (picked.length === 1 && onRemove) onRemove(picked[0], items.indexOf(picked[0]));
    else if (onRemoveMany) await onRemoveMany(picked);
    setSel(new Set());
    setSelecting(false);
  }

  const rowClass = (k) => "le-item" + (selecting && sel.has(k) ? " on" : "");

  return (
    <div className="mem-page">
      <div className="mem-head">
        <button className="mem-back" onClick={onBack}>{t("← 返回设置")}</button>
        <h2>{t(row.label)}
          {items.length ? <small>{t("（{n} 条）", { n: items.length })}</small> : null}
        </h2>
        <div className="log-acts">
          <button className={"skill-btn" + (selecting ? " on" : "")}
                  disabled={busy || !items.length}
                  onClick={pickAll}>{selecting ? t("取消多选") : t("多选")}</button>
        </div>
      </div>

      {selecting ? (
        <div className="le-selbar">
          <span>{t("已选 {n} 项", { n: sel.size })}</span>
          <button className="skill-btn skill-btn-danger"
                  disabled={busy || !connected || !sel.size}
                  onClick={askMany}>{t("删除选中")}</button>
        </div>
      ) : null}

      <div className="le-body">
        <div className="le-add">
          <input className="set-input" placeholder={row.ph ? t(row.ph) : ""}
                 value={input} disabled={busy || !connected}
                 onChange={(e) => setInput(e.target.value)}
                 onKeyDown={(e) => {
                   if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                     e.preventDefault();
                     submit();
                   }
                 }} />
          <button className="skill-btn"
                  disabled={busy || !connected || !input.trim()}
                  onClick={submit}>{t("＋ 添加")}</button>
          {row.allowlist === "computer_app" ? (
            <>
              <input id="wl-pick-list-editor" type="file" accept=".exe" hidden
                     onChange={(e) => {
                       const f = (e.target.files || [])[0];
                       if (f && f.name) setInput(f.name.toLowerCase());
                       e.target.value = "";
                     }} />
              <button className="skill-btn" type="button"
                      disabled={busy || !connected}
                      title={t("从磁盘选一个 exe —— 只会取它的文件名")}
                      onClick={() => {
                        const el = document.getElementById("wl-pick-list-editor");
                        if (el) el.click();
                      }}>{t("选择…")}</button>
            </>
          ) : null}
          {onReset ? (
            <button className="skill-btn" title={t("丢弃当前列表，恢复系统默认那一批")}
                    disabled={busy || !connected || !items.length}
                    onClick={onReset}>{t("↺ 恢复默认")}</button>
          ) : null}
        </div>

        <div className="le-list">
          {items.length === 0
            ? <span className="le-empty">{row.empty ? t(row.empty) : ""}</span>
            : isWatchers
              ? items.map((w, i) => {
                const k = keyOf(w, i, true);
                return (
                  <span className={rowClass(k)} key={k}
                        onClick={selecting ? () => toggleSel(k) : undefined}>
                    {selecting ? (
                      <input type="checkbox" checked={sel.has(k)} readOnly />
                    ) : null}
                    <i className={"wt-dot" + (w.running ? " on" : "")}
                       title={w.running ? t("运行中") : t("未运行")} />
                    <span className="le-val wt-path">{w.watch_path}</span>
                    {selecting ? null : (
                      <>
                        <button className={"switch" + (w.enabled ? " on" : "")}
                                role="switch" aria-checked={w.enabled ? "true" : "false"}
                                aria-label={t("启用这个监听")}
                                disabled={busy || !connected}
                                onClick={() => onToggleWt(w.id, !w.enabled)}>
                          <i />
                        </button>
                        <button className="wl-del" title={t("删掉它")}
                                disabled={busy || !connected}
                                onClick={() => askOne(w, i)}>✕</button>
                      </>
                    )}
                  </span>
                );
              })
              : items.map((s, i) => {
                const k = keyOf(s, i, false);
                return (
                  <span className={rowClass(k)} key={k}
                        onClick={selecting ? () => toggleSel(k) : undefined}>
                    {selecting ? (
                      <input type="checkbox" checked={sel.has(k)} readOnly />
                    ) : null}
                    {row.allowlist ? <code className="le-val">{s}</code>
                                   : <span className="le-val">{s}</span>}
                    {selecting ? null : (
                      <button className="wl-del"
                              title={row.allowlist ? t("删掉它，下次就会再问") : t("删掉它")}
                              disabled={busy || !connected}
                              onClick={() => askOne(s, i)}>✕</button>
                    )}
                  </span>
                );
              })}
        </div>
      </div>

      {ask ? (
        <Confirm
          title={ask.list.length === 1 ? t("删除这一条？")
                                       : t("删除选中的 {n} 项？", { n: ask.list.length })}
          body={ask.list.length === 1 ? String(ask.list[0].watch_path || ask.list[0])
                                      : t("这些条目会从名单里移除。")}
          confirmLabel={t("删除")}
          busy={busy}
          onConfirm={doDelete}
          onCancel={() => setAsk(null)} />
      ) : null}
    </div>
  );
}
