// 网络请求凭据（web_request 的 credential 条目）—— 设置里的独立子页。
//
// 判据：
//   · 值永不回显（GET 只给名字与头名）；编辑框留空表示"不改这一格"。
//   · 保存是合并（同名条目里给到的头覆盖，没给到的保留）。
//   · 删除要按第二次（与记忆库同一条：不可逆的动作不靠一次误点）。
import React, { useEffect, useState } from "react";
import { jsonRequest } from "./api.js";
import { t } from "./i18n.js";

function parseHeaders(text) {
  const out = {};
  String(text || "").split(/\r?\n/).forEach((line) => {
    const s = line.trim();
    if (!s) return;
    const i = s.indexOf(":");
    if (i <= 0) return;
    out[s.slice(0, i).trim()] = s.slice(i + 1).trim();
  });
  return out;
}

export function NetCreds({ client, notify, onBack }) {
  const [entries, setEntries] = useState([]);
  const [sel, setSel] = useState("");          // 当前编辑的条目名（空 = 新建）
  const [name, setName] = useState("");
  const [text, setText] = useState("");
  const [confirmDel, setConfirmDel] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const r = await client.call("/api/web-request/credentials");
      setEntries((r && r.entries) || []);
    } catch (e) { notify(e.message); }
  }

  useEffect(() => { load(); }, []);            // eslint-disable-line

  function pick(entry) {
    setSel(entry.name);
    setName(entry.name);
    setText("");                                // 值不回显：只能重填
    setConfirmDel("");
  }

  async function save() {
    const headers = parseHeaders(text);
    if (!String(name).trim()) { notify(t("名字不能为空")); return; }
    if (!Object.keys(headers).length) { notify(t("至少要有一个头（Header: value）")); return; }
    setBusy(true);
    try {
      await client.call("/api/web-request/credentials",
        jsonRequest("POST", { name: String(name).trim(), headers }));
      notify(t("已保存。"));
      await load();
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  async function del() {
    if (confirmDel !== sel) { setConfirmDel(sel); return; }
    setBusy(true);
    try {
      await client.call("/api/web-request/credentials?name=" + encodeURIComponent(sel),
        jsonRequest("DELETE", {}));
      notify(t("已删除。"));
      setSel(""); setName(""); setText(""); setConfirmDel("");
      await load();
    } catch (e) { notify(e.message); }
    finally { setBusy(false); }
  }

  return (
    <section className="settings netcreds">
      <div className="mem-head">
        <button className="skill-btn" onClick={onBack}>{t("返回")}</button>
        <b>{t("网络请求凭据")}</b>
      </div>
      <p className="set-note">
        {t("给 web_request 用的命名凭据：模型只会获取条目名，具体值由框架注入")}
      </p>
      <div className="netcreds-body">
        <div className="netcreds-list">
          {entries.length === 0 ? (
            <div className="set-note">{t("（还没有条目）")}</div>
          ) : entries.map((e) => (
            <button key={e.name}
                    className={"netcreds-item" + (e.name === sel ? " on" : "")}
                    onClick={() => pick(e)}>
              <b>{e.name}</b>
              <span>{(e.headers || []).join(" / ")}</span>
            </button>
          ))}
          <button className="skill-btn" onClick={() => {
            setSel(""); setName(""); setText(""); setConfirmDel("");
          }}>{t("新建条目")}</button>
        </div>
        <div className="netcreds-edit">
          <label>{t("条目名")}
            <input value={name} disabled={!!sel}
                   onChange={(ev) => setName(ev.target.value)} />
          </label>
          <label>{t("请求头（每行一个：Header: value）")}
            <textarea rows={6} value={text}
                      placeholder={"Authorization: Bearer ...\nX-Api-Key: ..."}
                      onChange={(ev) => setText(ev.target.value)} />
          </label>
          <div className="set-note">
            {t("已保存的值不会显示；留空的字段表示不修改。")}
            {sel ? " " + t("当前头：") + (entries.find((x) => x.name === sel) || {}).headers?.join(" / ") : ""}
          </div>
          <div className="netcreds-actions">
            <button className="skill-btn" disabled={busy} onClick={save}>{t("保存")}</button>
            {sel ? (
              <button className="skill-btn danger" disabled={busy} onClick={del}>
                {confirmDel === sel ? t("确认删除") : t("删除")}
              </button>
            ) : null}
          </div>
        </div>
      </div>
    </section>
  );
}
