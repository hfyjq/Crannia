// 技能的声明式配置卡（步 3；第一个用户 = 生图 skill）。
//
// 判据：这张卡不认识任何具体技能。字段名、标题、类型、哪些是敏感字段，
//    全部来自 `manifest.yaml` 的 `settings`（后端 `GET /api/skills/config?name=`
//    会一并交出来）。加一个带配置的新 skill = 加一份 manifest，不改这里。
//
// 形状照 `mail_parser` 那张卡（用户指定）：
//    · 已填好的条目折叠成一行（名字 + 默认 + 修改 + 删除），点「修改」才展开；
//    · 新建也是行内起名（先名字、再字段）；
//    · 删除垫红底 + 二次确认（红底用现成的 `.skill-btn-danger`）。
//
// 不许用 `window.prompt` / `window.confirm`：桌面壳里它们直接抛
//    （`[renderer] Uncaught Error: prompt() is not supported` —— 用户
//    点「新建条目」没反应就是这个）。所有输入/确认都必须画在页面里。
//
// 敏感字段（`x-secret`）后端只给 ``；用户不改它时原样送回，
//    后端负责换回旧值（`core/skill_settings._keep_masked`）。
//    界面不许把 `` 变成空串 —— 那等于把用户的密钥清掉。
//
// 零 `innerHTML`：这里显示的字段值可能是用户手写进配置文件的文本。

import { useCallback, useEffect, useMemo, useState } from "react";

import { t } from "./i18n.js";
import {
  MASK, blankEntry, entryFieldsOf, payloadOf, viewOf,
} from "./skill-config-model.js";

/** 一个字段的输入控件；`secret` 的带一颗显示 / 隐藏的眼睛。
 *
 *  眼睛点开时，如果手里的是打码值 ``，先向服务端要一次明文
 *    （`?reveal=1`）再显示 —— 否则用户看到的还是四个星号。
 *  眼睛只改 `type` 与这一格的值；不参与"是不是新值"的判断
 *    （没点开的打码值仍然原样送回）。
 */
function FieldInput({ field, value, onChange, disabled, onReveal }) {
  const [revealed, setRevealed] = useState(false);
  const secret = field.secret || field.format === "password";
  const common = {
    className: "set-input", value: value == null ? "" : String(value),
    disabled,
    onChange: (e) => onChange(e.target.value),
  };
  if (field.options.length) {
    return (
      <select {...common}>
        {field.options.map((o) => <option value={o} key={o}>{o}</option>)}
      </select>
    );
  }
  if (!secret) {
    return (
      <input {...common} type="text"
             placeholder={field.description ? t(field.description) : ""} />
    );
  }
  const toggle = async () => {
    //: 要显示、而手里是打码值，先要明文（拿不到就只翻 type，不假装拿到了）
    if (!revealed && value === MASK && onReveal) {
      try { await onReveal(); } catch { /* 拿不到就照旧显示打码值 */ }
    }
    setRevealed((v) => !v);
  };
  return (
    <span className="skill-cfg-secret">
      <input {...common} type={revealed ? "text" : "password"}
             autoComplete="off"
             placeholder={field.description ? t(field.description) : ""} />
      <button type="button" className="skill-cfg-eye"
              disabled={disabled}
              title={revealed ? t("隐藏") : t("显示")}
              aria-label={revealed ? t("隐藏") : t("显示")}
              onClick={toggle}>
        {revealed ? "◉" : "◎"}
      </button>
    </span>
  );
}

/** 一行字段（label + 控件），展开态与新建态共用。 */
function Fields({ fields, values, onChange, disabled, onReveal }) {
  return (
    <>
      {fields.map((f) => (
        <label className="skill-cfg-field" key={f.name}>
          <span className="skill-cfg-label">{t(f.title)}</span>
          <FieldInput field={f} disabled={disabled}
                      value={values[f.name] == null ? "" : values[f.name]}
                      onChange={(v) => onChange(f.name, v)}
                      onReveal={onReveal ? () => onReveal(f.name) : undefined} />
        </label>
      ))}
    </>
  );
}

export default function SkillConfig({ skill, client, notify, connected }) {
  const [data, setData] = useState(null);      // {schema} —— 只有它决定画什么
  const [view, setView] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  //:正在展开编辑的条目名（`""` = 没在编辑）。折叠态是默认。
  const [editing, setEditing] = useState("");
  //:正在新建时的名字（`null` = 没在新建）。
  const [newName, setNewName] = useState(null);
  //: 编辑器里的草稿（新建与修改共用一格 —— 同时只会有一个在编辑）。
  const [form, setForm] = useState({});
  //:等着二次确认的删除目标（`""` = 没有）。
  const [confirmDel, setConfirmDel] = useState("");

  //:名字走查询串，路径是字面量 —— 前端写 `/api/skills/<名字>/config`
  //:   这种动态串的话，`test_stars_ui` 的对账器会把它抽成 `/api/skills/`
  //:，当成"服务端没有这条路由"当场红（那条读者是按字面量抓的）。
  const cfgUrl = "/api/skills/config?name=" + encodeURIComponent(skill);

  const reload = useCallback(async () => {
    try {
      const d = await client.call(cfgUrl);
      const schema = (d && d.schema) || {};
      setData({ schema });
      setView(viewOf((d && d.config) || {}, schema));
      setErr("");
    } catch (e) {
      setErr(e && e.message ? e.message : String(e));
    }
  }, [client, cfgUrl]);

  useEffect(() => { reload(); }, [reload]);

  const entryFields = useMemo(
    () => entryFieldsOf((data && data.schema) || {}), [data]);

  if (!view) {
    return <div className="set-row col" key={skill}>
      <b>{t("生图与视频")}</b>
      <span>{err ? t(err) : t("正在读取配置…")}</span>
    </div>;
  }

  const save = async (entries, defaultName) => {
    setBusy(true);
    try {
      await client.call(cfgUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payloadOf({
          key: view.key, entries, defaultName,
        })),
      });
      notify(t("已保存"));
      setEditing(""); setNewName(null); setForm({}); setConfirmDel("");
      await reload();
      return true;
    } catch (e) {
      notify(e && e.message ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  /** 设为默认 —— 立刻存（与邮箱那张卡同一个手感，不留一个"忘了保存"）。 */
  const makeDefault = (name) => save(view.entries, name);

  const startEdit = (name) => {
    setNewName(null); setConfirmDel("");
    setEditing(name);
    setForm({ ...(view.entries[name] || {}) });
  };

  const startNew = () => {
    setEditing(""); setConfirmDel("");
    setNewName("");
    setForm(blankEntry(entryFields));
  };

  const commit = async () => {
    const name = (editing || String(newName || "")).trim();
    if (!name) { notify(t("先给这个条目起个名字")); return; }
    if (!editing && Object.prototype.hasOwnProperty.call(view.entries, name)) {
      notify(t("已经有叫「{name}」的条目了", { name }));
      return;
    }
    const entries = { ...view.entries, [name]: { ...form } };
    await save(entries, view.defaultName || name);
  };

  const del = async (name) => {
    const entries = { ...view.entries };
    delete entries[name];
    const def = view.defaultName === name ? (Object.keys(entries)[0] || "") : view.defaultName;
    await save(entries, def);
  };

  const setField = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  /** 眼睛要明文：只在用户明确点开时要一次（`?reveal=1`），不预取。 */
  const revealField = async (fieldName) => {
    if (!editing) return;                 // 新建态没有"旧值"可取
    const d = await client.call(cfgUrl + "&reveal=1");
    const ent = (((d || {}).config || {})[view.key] || {})[editing] || {};
    const v = ent[fieldName];
    if (typeof v === "string") setField(fieldName, v);
  };
  const names = Object.keys(view.entries);
  const editingNow = !!editing || newName !== null;

  return (
    <div className="set-row col" key={skill}>
      <div>
        <b>{t("生图与视频")}</b>
        <span>{t("这些条目由生图 skill 使用")}</span>
      </div>
      <div className="skill-cfg">
        {names.length === 0 && newName === null
          ? <span className="wl-empty">{t("还没有配置条目")}</span>
          : names.map((name) => (
            <div className={"skill-cfg-entry" + (view.defaultName === name ? " is-default" : "")}
                 key={name}>
              {/* 折叠态：一行（默认 + 名字 + 修改 + 删除）。 */}
              <div className="skill-cfg-head">
                <label title={t("不指定条目时用这一条")}>
                  <input type="radio" name={"def-" + skill}
                         checked={view.defaultName === name}
                         disabled={busy || !connected || editingNow}
                         onChange={() => makeDefault(name)} />
                  <code>{name}</code>
                </label>
                <span className="skill-cfg-actions-row">
                  <button className="skill-btn" disabled={busy || !connected || editingNow}
                          onClick={() => startEdit(name)}>{t("修改")}</button>
                  {confirmDel === name ? (
                    <>
                      <span className="skill-cfg-warn">{t("确认删除？")}</span>
                      <button className="skill-btn skill-btn-danger"
                              disabled={busy || !connected}
                              onClick={() => del(name)}>{t("删除")}</button>
                      <button className="skill-btn" disabled={busy}
                              onClick={() => setConfirmDel("")}>{t("取消")}</button>
                    </>
                  ) : (
                    <button className="skill-btn skill-btn-danger cfg-del"
                            title={t("删除这条配置")}
                            disabled={busy || !connected || editingNow}
                            onClick={() => setConfirmDel(name)}>✕</button>
                  )}
                </span>
              </div>
              {/* 展开态：只有正在改的这一条长开。 */}
              {editing === name ? (
                <>
                  <Fields fields={entryFields} values={form} onChange={setField}
                          disabled={busy || !connected} onReveal={revealField} />
                  <div className="skill-cfg-actions">
                    <button className="skill-btn" disabled={busy}
                            onClick={() => { setEditing(""); setForm({}); }}>{t("取消")}</button>
                    <button className="skill-btn primary" disabled={busy || !connected}
                            onClick={commit}>{busy ? t("正在保存…") : t("保存")}</button>
                  </div>
                </>
              ) : null}
            </div>
          ))}

        {/* 新建：行内起名（不许 `prompt` —— 桌面壳里它会直接抛）。 */}
        {newName !== null ? (
          <div className="skill-cfg-entry is-default">
            <div className="skill-cfg-head">
              <label>
                <span>{t("条目名")}</span>
                <input className="set-input" value={newName} autoFocus
                       disabled={busy || !connected}
                       placeholder={t("比如 ark / 本地")}
                       onChange={(e) => setNewName(e.target.value)} />
              </label>
            </div>
            <Fields fields={entryFields} values={form} onChange={setField}
                    disabled={busy || !connected} />
            <div className="skill-cfg-actions">
              <button className="skill-btn" disabled={busy}
                      onClick={() => { setNewName(null); setForm({}); }}>{t("取消")}</button>
              <button className="skill-btn primary"
                      disabled={busy || !connected || !String(newName || "").trim()}
                      onClick={commit}>{busy ? t("正在保存…") : t("保存")}</button>
            </div>
          </div>
        ) : null}
      </div>
      <div className="skill-cfg-actions">
        <button className="skill-btn" disabled={busy || !connected || editingNow}
                onClick={startNew}>{t("＋ 新建条目")}</button>
      </div>
      {err ? <span className="muted">{t(err)}</span> : null}
    </div>
  );
}
