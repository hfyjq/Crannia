import React, { useEffect, useState } from "react";
import { RefreshCw, Square, X } from "lucide-react";
import { jsonRequest } from "./api.js";
import {
  SELECTABLE_CHANNELS, channelById, channelIconUrl, channelNotice,
  channelStatusOf, setDefaultBody, startBody,
} from "./channels-model.js";
import { WechatLogin } from "./WechatLogin.jsx";

// 「端配置」页的通道卡（从旧 WebUI 的端配置页搬过来）。
//
// 判据在 `channels-model.js`（纯函数，node 逐条断言）—— 这里只管画。
//
// 不使用后端返回的 `channels[].icon` —— 它是一段 HTML 字符串
//    （`_detect_channels` 里拼的 `<img …>`）。图标表只在本仓库这一处，
//    路径只从 `client.asset` 出（见 `channels-model.js::channelIconUrl`）。
//
// 微信那一支不调 `/api/restart`：扫码成功后 `channel.run()` 已经在跑；
//    重启既停不掉旧线程、又可能再起一个（同 `WechatLogin.jsx` 抬头那段）。
//
// 保存通道凭据走 `POST /api/config {section:"channel"}`；「启动」是
//    `POST /api/channel/start` —— 它要求框架已经在跑，否则后端回 400。
//    那一句照实显示，不许乐观地写「运行中」。

import { t } from "./i18n.js";
export function Channels({ client, notify, connected }) {
  const [cfg, setCfg] = useState(null);
  const [running, setRunning] = useState({});
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [openId, setOpenId] = useState("");
  const [forms, setForms] = useState({});
  const [notice, setNotice] = useState("");
  //: 用户按过「保存配置」没有。校验错误只在按过之后才显示 ——
  //:   不然一个已配置的通道一打开就是红的（输入框按规矩是空的，
  //:   而空着的规则是「至少填一格」），等于把「还没填」说成「填错了」。
  const [attempted, setAttempted] = useState(false);
  //: 配置条目那三格：正在编辑哪一条（`""` = 新建）、条目名、待确认删除的 id。
  const [editingId, setEditingId] = useState("");
  const [entryName, setEntryName] = useState("");
  const [confirmEntry, setConfirmEntry] = useState("");
  //: 上次启动失败的原因（`{通道 id: {error, errorTpl, errorVars}}`）。
  //:   与 `notice` 分开：`notice` 是「点完那一下」的回执，一转头就没了；
  //:   失败状态是一直在的，直到这条通道真的起来（后端 `resolve()` 撤掉它）。
  const [errors, setErrors] = useState({});

  async function load() {
    if (!client) return;
    try {
      const [c, s] = await Promise.allSettled([
        client.call("/api/config"),
        client.call("/api/channel/status"),
      ]);
      if (c.status === "fulfilled") {
        setCfg(c.value || null);
        setErr((c.value && c.value.error) || "");
      } else {
        setCfg({ channels: [] });
        setErr(c.reason && c.reason.message ? c.reason.message : t("读取通道配置失败"));
      }
      if (s.status === "fulfilled") {
        setRunning((s.value && s.value.running) || {});
        setErrors((s.value && s.value.errors) || {});
      }
    } catch (e) {
      setCfg({ channels: [] });
      setErr(e.message);
    }
  }

  //:只刷状态（不刷配置 —— 每 5 秒读一次 `config.yaml` 没有意义）。
  async function refreshStatus() {
    if (!client) return;
    try {
      const s = await client.call("/api/channel/status");
      setRunning((s && s.running) || {});
      setErrors((s && s.errors) || {});
    } catch (e) { /* 状态读不到就保持上一次；下面那一行会照实标 */ }
  }

  useEffect(() => { load(); }, [client]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const timer = setInterval(refreshStatus, 5000);
    return () => clearInterval(timer);
  }, [client]);                              // eslint-disable-line react-hooks/exhaustive-deps

  const rows = (cfg && Array.isArray(cfg.channels)) ? cfg.channels : [];
  const rowOf = (id) => rows.find((r) => r && r.id === id) || null;
  const def = channelById(openId);
  const openRow = rowOf(openId);
  const openRunning = !!running[openId];
  const form = forms[openId] || {};
  //: 这一个通道已存的几套凭据（后端按账本给，凭据已打码）。
  const entries = (openRow && Array.isArray(openRow.entries)) ? openRow.entries : [];
  //: 表单现在在改哪一条（`""` = 新建一条）。
  const entryFilled = Object.values(form).some((v) => String(v || "").trim());
  const nameOk = !!String(entryName || "").trim();
  //: 新建要有名字加至少一格凭据；改一条只要名字（凭据留空 = 不改）。
  const valid = !def ? { ok: true }
    : def.kind !== "form" ? { ok: true }
      : (nameOk && (!!editingId || entryFilled))
        ? { ok: true }
        : { ok: false,
            error: !nameOk ? t("条目名不能为空")
                           : t("新建一条时至少要填一格凭据") };

  //:后端若新增了本表里没有的通道：追加通用卡，不丢掉（也不崩）。
  const extras = rows.filter((r) => r && !channelById(r.id));

  //:接入前的提示（今天只有飞书有）：先弹一句，再开配置页。
  //:`noticed` 是本次运行内的确认记录 —— 同一进程里反复改配置不再打断。
  const [warnId, setWarnId] = useState("");
  const [noticed, setNoticed] = useState({});

  function openModal(id) {
    const n = channelNotice(id);
    if (n && !noticed[id]) {
      setWarnId(id);
      return;
    }
    _openModal(id);
  }

  function _openModal(id) {
    setOpenId(id);
    setNotice("");
    setErr("");
    setAttempted(false);
    setEditingId("");
    setEntryName("");
    setConfirmEntry("");
    if (!forms[id]) {
      const d = channelById(id);
      const init = {};
      for (const f of (d && d.fields) || []) init[f.key] = "";
      setForms((x) => ({ ...x, [id]: init }));
    }
  }

  function close() {
    setOpenId("");
    setNotice("");
    setAttempted(false);
    setEditingId("");
    setEntryName("");
    setConfirmEntry("");
  }

  async function act(path, body, okMsg) {
    setBusy(openId || "load");
    setNotice("");
    try {
      const r = await client.call(path, jsonRequest("POST", body));
      const msg = (r && (r.message || r.error)) || okMsg;
      setNotice(msg);
      if (r && r.ok === false) setErr(r.error || msg || t("操作失败"));
      await load();
      return r;
    } catch (e) {
      setErr(e.message);
      if (typeof notify === "function" && e.message) notify(e.message);
      return null;
    } finally {
      setBusy("");
    }
  }

  async function saveChannel() {
    if (!def) return;
    if (!valid.ok) {
      //: 按了才知道缺什么 —— 按钮不再一上来就灰着，
      //:   否则这句话永远没有机会出来。
      setAttempted(true);
      return;
    }
    const editing = !!editingId;
    await act(editing ? "/api/channel/entry/update" : "/api/channel/entry/create",
              editing ? { id: editingId, name: entryName, fields: form }
                      : { channel_id: openId, name: entryName, fields: form },
              t("配置已保存。"));
    resetEntryForm();
    //: 存完输入框清空，`valid` 会重新变回「没填」；不复位的话红字会
    //:   在保存成功之后冒出来 —— 那条提示说的是"还没改"，与刚发生的事相反。
    setAttempted(false);
  }

  /** 把表单退回「新建一条」的状态。 */
  function resetEntryForm() {
    setForms((x) => ({ ...x, [openId]: {} }));
    setEditingId("");
    setEntryName("");
  }

  function startEditEntry(entry) {
    setEditingId(entry.id);
    setEntryName(entry.name || "");
    //: 凭据不回显，所以编辑态下这一格永远是空的 —— 留空就表示不改。
    setForms((x) => ({ ...x, [openId]: {} }));
    setAttempted(false);
  }

  function setEntryDefault(id) {
    return act("/api/channel/entry/default", { channel_id: openId, id },
               t("已设为默认；重启框架后生效。"));
  }

  async function delEntry() {
    const id = confirmEntry;
    if (!id) return;
    setConfirmEntry("");
    await act("/api/channel/entry/delete", { id }, t("已删除。"));
    if (editingId === id) resetEntryForm();
  }

  async function startChannel() {
    if (!def) return;
    const r = await act("/api/channel/start", startBody(openId),
                        t("通道已启动。"));
    if (r && r.status === "already_running") setNotice(t("通道已经在运行。"));
  }

  //:停止 / 重启（Telegram 的循环停了，而界面上
  //:根本没有停止按钮，用户唯一的出路是把整个应用掐掉）。
  //:判据：`running: true` 是标记，不是"真的在收"。
  async function stopChannel(id) {
    const cid = id || openId;
    if (!cid) return null;
    setBusy(cid);
    setNotice("");
    setErr("");
    try {
      const r = await client.call("/api/channel/stop",
                                  jsonRequest("POST", startBody(cid)));
      if (r && r.ok === false) {
        setErr(r.error || t("这条通道停不下来，请重启应用。"));
      } else {
        const st = (r && r.status) || "";
        setNotice(st === "not_running" ? t("这条通道本来就没在运行。")
                 : st === "forced" ? t("通道已停止（强制）。")
                 : t("通道已停止。"));
      }
      await load();
      return r;
    } catch (e) {
      setErr(e.message);
      return null;
    } finally {
      setBusy("");
    }
  }

  //:卡片上直接启动，右下角不再摆「运行中」那颗字。
  //:与弹窗那颗同一个端点、同一句回话 —— 不新开一条路。
  //:失败必须说清为什么（微信没登录时后端回 400）：卡片没有弹窗的提示位，
  //:   所以走 `notice` —— 弹窗关着时它显示在页面下方（`{notice && !openId}`）。
  async function startChannelFor(id) {
    if (!id) return null;
    setBusy(id);
    setNotice("");
    setErr("");
    try {
      const r = await client.call("/api/channel/start",
                                  jsonRequest("POST", startBody(id)));
      const st = (r && r.status) || "";
      setNotice(st === "already_running"
                ? t("通道已经在运行。")
                : ((r && (r.message || r.error)) || t("通道已启动。")));
      await load();
      return r;
    } catch (e) {
      setErr(e.message);
      return null;
    } finally {
      setBusy("");
    }
  }

  async function setDefault() {
    if (!def) return;
    await act("/api/channel/set-default", setDefaultBody(openId),
              t("默认通道已保存（下次启动时生效）。"));
  }

  //:卡片上直接设默认 —— 只有弹窗那一处入口时，不点开卡片就找不到它，
  //:   于是每次启动都固定拉起第一条通道。判据与弹窗里那颗同一个端点、同一句回话 ——
  //:   不新开一条路，只是把入口摆到不需要点开卡片就能看见的地方。
  //:回话必须说清「下次启动时生效」：它不热切正在跑的那条通道。
  async function setDefaultFor(id) {
    if (!id) return;
    setBusy(id);
    setNotice("");
    setErr("");
    try {
      //:必须包 `jsonRequest("POST", …)` —— 直接传 body 字典时
      //:`client.call` 用默认方法（GET），而这个端点只收 POST，405。
      //:   （点「设为默认」页面回「请求失败（405）」。）
      const r = await client.call("/api/channel/set-default",
                                  jsonRequest("POST", setDefaultBody(id)));
      const msg = (r && (r.message || r.error)) || t("默认通道已保存（下次启动时生效）。");
      setNotice(msg);
      if (r && r.ok === false) setErr(r.error || msg || t("操作失败"));
      await load();                    // 重新读 config，角标立刻换到新那一张
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy("");
    }
  }

  if (!connected) {
    return (
      <section className="ch-section">
        <div className="ch-head"><div><small>CHANNELS</small><h2>{t("通道")}</h2></div></div>
        <p className="ch-note">{t("启动本地服务后配置微信 / 飞书 / 钉钉 / Telegram。")}</p>
      </section>
    );
  }

  return (
    <section className="ch-section">
      <div className="ch-head">
        <div><small>CHANNELS</small><h2>{t("通道")}</h2></div>
        <button onClick={load} disabled={!connected}><RefreshCw size={15} />{t("刷新")}</button>
      </div>
      <p className="ch-note">{t("把 Agent 接到微信、飞书、钉钉或 Telegram。")}</p>
      {/* 多个通道可以同时运行（各自一条线程，共用一套管线）；
          这一行是为了让"两张卡都是运行中"看起来是正常的，而不是以为界面错了。 */}
      <p className="ch-note">{t("可以同时运行多个通道（运行中的有绿框）；「默认通道」是下次启动时自动拉起的那一个。")}</p>

      {cfg === null && <p className="ch-note">{t("读取中…")}</p>}
      {err && <p className="ch-error">{err}</p>}
      {notice && !openId && <p className="ch-hint">{notice}</p>}
      {/* 启动失败：顶部也来一条（显眼），一直留到那条通道真的起来，
          与上面 `notice` 那个"一转头就没"的回执不是一件事。 */}
      {!openId && (() => {
        const _ids = Object.keys(errors);
        if (!_ids.length) return null;
        const _id = _ids[0];
        const _e = errors[_id] || {};
        const _d = SELECTABLE_CHANNELS.find((c) => c.id === _id) || {};
        return (
          <p className="ch-error">
            {t("「{name}」启动失败：{msg}", {
              name: t(_d.label || _id),
              msg: _e.errorTpl ? t(_e.errorTpl, _e.errorVars) : (_e.error || ""),
            })}
          </p>
        );
      })()}

      <div className="ch-cards">
        {SELECTABLE_CHANNELS.map((d, i) => {
          const row = rowOf(d.id);
          const st = channelStatusOf(d, row, !!running[d.id]);
          const icon = channelIconUrl(client, d.id);
          const isDefault = !!(cfg && cfg.channel === d.id);
          return (
            //: 外层是 div 不是 button —— 里面要放「设为默认」这颗按钮，
            //:   `<button>` 套 `<button>` 是非法 HTML（浏览器会拆开它们）。
            <div className={"ch-card" + (running[d.id] ? " ch-card-running" : "")
                     + (isDefault ? " ch-card-active" : "")}
                 style={{ animationDelay: (i * 0.05) + "s" }}
                 key={d.id}>
              {/* 停止在右上角（用户指定位置）。
                  与 `.ch-card-main` 平级 —— 那颗是 button，不能套 button。 */}
              {running[d.id] && (
                <button className="ch-stop-btn" disabled={!!busy}
                        title={t("停止通道")} aria-label={t("停止")}
                        onClick={(ev) => { ev.stopPropagation(); stopChannel(d.id); }}>
                  <Square size={11} strokeWidth={2.6} />
                </button>
              )}
              <button className="ch-card-main" onClick={() => openModal(d.id)}
                      title={isDefault ? t("默认通道（下次启动时拉起）") : ""}>
                <span className="ch-logo">
                  {icon ? <img src={icon} alt="" /> : <span aria-hidden="true">🖥</span>}
                </span>
                <b>{t(d.label)}</b>
                <span className={"ch-status " + st.cls}>{st.label}</span>
              </button>
              {/* 上次启动失败的原因就长在这张卡上：用户点的是它、失败的也是它，
                  不用去别处找对应关系。 */}
              {errors[d.id] ? (
                <p className="ch-card-err">
                  {errors[d.id].errorTpl
                    ? t(errors[d.id].errorTpl, errors[d.id].errorVars)
                    : errors[d.id].error}
                </p>
              ) : null}
              <div className="ch-card-foot">
                {isDefault
                  ? <span className="ch-default-tag">{t("默认通道")}</span>
                  : <button className="ch-default-btn" disabled={!!busy}
                            onClick={() => setDefaultFor(d.id)}>
                      {t("设为默认")}
                    </button>}
                {/* 右下角原来是「运行中」那颗字 —— 它和卡里那行状态重复，
                    而这里更该放动作，所以换成启动按钮。
                    运行中时这里不出现（停止在右上角那颗 ⏹）。 */}
                {!running[d.id] && (
                  <button className="ch-start-btn"
                          disabled={!!busy || !(row && row.configured)}
                          title={(row && row.configured) ? t("启动通道")
                                 : (d.kind === "wechat"
                                    ? t("先扫码登录") : t("先保存配置"))}
                          onClick={() => startChannelFor(d.id)}>{t("启动")}</button>
                )}
              </div>
            </div>
          );
        })}
        {extras.map((r, j) => {
          const isDefault = !!(cfg && cfg.channel === r.id);
          return (
            <div className={"ch-card" + (running[r.id] ? " ch-card-running" : "")
                     + (isDefault ? " ch-card-active" : "")}
                 key={r.id}
                 style={{ animationDelay: ((SELECTABLE_CHANNELS.length + j) * 0.05) + "s" }}>
              {running[r.id] && (
                <button className="ch-stop-btn" disabled={!!busy}
                        title={t("停止通道")} aria-label={t("停止")}
                        onClick={(ev) => { ev.stopPropagation(); stopChannel(r.id); }}>
                  <Square size={11} strokeWidth={2.6} />
                </button>
              )}
              <button className="ch-card-main" onClick={() => openModal(r.id)}>
                <span className="ch-logo"><span aria-hidden="true">🔌</span></span>
                <b>{r.id}</b>
                <span className={"ch-status " + (r.configured ? "ch-status-ok" : "ch-status-warn")}>
                  {running[r.id] ? t("运行中") : (r.configured ? t("已配置") : t("未配置"))}
                </span>
              </button>
              <div className="ch-card-foot">
                {isDefault
                  ? <span className="ch-default-tag">{t("默认通道")}</span>
                  : <button className="ch-default-btn" disabled={!!busy}
                            onClick={() => setDefaultFor(r.id)}>{t("设为默认")}</button>}
                {!running[r.id] && (
                  <button className="ch-start-btn" disabled={!!busy || !r.configured}
                          title={r.configured ? t("启动通道") : t("先保存配置")}
                          onClick={() => startChannelFor(r.id)}>{t("启动")}</button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {warnId && (() => {
        const n = channelNotice(warnId);
        return n ? (
          <div className="backdrop"
               onMouseDown={(e) => e.target === e.currentTarget && setWarnId("")}>
            <section className="ch-panel" role="dialog" aria-modal="true">
              <div className="settings-head">
                <div><small>{n.title}</small><h2>{t((channelById(warnId) || {}).label || "")}</h2></div>
                <button className="round" onClick={() => setWarnId("")}><X size={18} /></button>
              </div>
              <p className="ch-note">{n.body}</p>
              <div className="ch-actions">
                <button className="skill-btn"
                        onClick={() => {
                          const id = warnId;
                          setNoticed((x) => ({ ...x, [id]: true }));
                          setWarnId("");
                          _openModal(id);
                        }}>{n.ok}</button>
                <button className="skill-btn" onClick={() => setWarnId("")}>{n.cancel}</button>
              </div>
            </section>
          </div>
        ) : null;
      })()}

      {openId && (
        <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && close()}>
          <section className="ch-panel" role="dialog" aria-modal="true">
            <div className="settings-head">
              <div><small>{t("通道配置")}</small><h2>{def ? t(def.label) : openId}</h2></div>
              <button className="round" onClick={close}><X size={18} /></button>
            </div>

            {!def && (
              <p className="ch-note">
                {t("这个通道类型本界面还不认识（id: {openId}）。可以到设置里看它的状态， 但这里不提供配置表单。",
                   { openId })}
              </p>
            )}

            {def && def.kind === "cli" && (
              <p className="ch-note">{t("CLI 为终端式对话，始终可用，无需配置。")}</p>
            )}

            {def && def.kind === "form" && (
              <div className="ch-form">
                {/* 已存的几套凭据。一套一行，与端配置页同一条语法：
                    条目在上一栏，表单在下面，改哪条就点哪条的「修改」。 */}
                <div className="ep-list">
                  {!entries.length && (
                    <p className="muted">{t("还没有配置 —— 在下面新建一条。")}</p>
                  )}
                  {entries.map((e) => (
                    <div className={"ep-row" + (e.is_default ? " ep-row-default" : "")} key={e.id}>
                      <div className="ep-row-main">
                        <b>{e.name}</b>
                        {e.is_default && <span className="ep-badge">{t("默认启用")}</span>}
                        <span className="muted">
                          {Object.keys(e.fields || {})
                            .map((k) => t(k) + "：" + e.fields[k]).join(" · ")}
                        </span>
                      </div>
                      <div className="ep-row-acts">
                        {!e.is_default && (
                          <button className="skill-btn" disabled={!!busy}
                                  title={t("通道下次启动时用它（要重启框架）")}
                                  onClick={() => setEntryDefault(e.id)}>{t("设为默认")}</button>
                        )}
                        <button className="skill-btn" disabled={!!busy}
                                onClick={() => startEditEntry(e)}>{t("修改")}</button>
                        <button className="skill-btn skill-btn-danger"
                                disabled={!!busy || e.is_default}
                                title={e.is_default
                                       ? t("默认那条不能删 —— 先把另一条设为默认")
                                       : t("删掉这条配置")}
                                onClick={() => setConfirmEntry(e.id)}>{t("删除")}</button>
                      </div>
                    </div>
                  ))}
                </div>

                <p className="ch-hint">
                  {editingId ? t("改这一条；凭据留空表示不改。")
                             : t("填好条目名与凭据再保存。")}
                </p>
                <label className="field">
                  <span>{t("条目名")}</span>
                  <input value={entryName}
                         placeholder={t("比如：工作号 / 测试号")}
                         onChange={(e) => setEntryName(e.target.value)} />
                </label>
                {(def.fields || []).map((f) => {
                  //: 已保存的指纹（后端给，已打码）。输入框一律是空的
                  //   （留空 = 不改），光看它分不出"改过没改" —— 这一行就是那个区分。
                  const edited = entries.find((e) => e.id === editingId) || null;
                  const saved = (edited && edited.fields && edited.fields[f.key]) || "";
                  return (
                    <label className="field" key={f.key}>
                      <span>{t(f.label)}</span>
                      <input type={f.secret ? "password" : "text"}
                             value={form[f.key] == null ? "" : form[f.key]}
                             placeholder={editingId ? t("留空 = 不改")
                                                    : t("输入 {f.label}…", { "f.label": t(f.label) })}
                             onChange={(e) => setForms((x) => ({
                               ...x, [openId]: { ...(x[openId] || {}), [f.key]: e.target.value },
                             }))} />
                      {saved
                        ? <em className="ch-saved">{t("已保存：{value}", { value: saved })}</em>
                        : null}
                    </label>
                  );
                })}
                {attempted && !valid.ok && <p className="ch-error">{valid.error}</p>}
                {confirmEntry && (
                  <p className="ch-hint">
                    {t("确认删除？")}
                    {" "}
                    <button className="skill-btn skill-btn-danger" onClick={delEntry}>{t("删除")}</button>
                    {" "}
                    <button className="skill-btn" onClick={() => setConfirmEntry("")}>{t("取消")}</button>
                  </p>
                )}
              </div>
            )}

            {def && def.kind === "wechat" && (
              <WechatLogin client={client} connected={connected}
                           configured={!!(openRow && openRow.configured)}
                           onConfirmed={() => { setNotice(t("微信登录成功。")); load(); }}
                           onStart={() => setNotice("")} />
            )}

            {notice && <p className="ch-hint">{notice}</p>}
            {err && <p className="ch-error">{err}</p>}

            <div className="ch-actions">
              {def && def.kind === "form" && (
                <button className="skill-btn" disabled={!!busy}
                        onClick={saveChannel}>
                  {editingId ? t("保存修改") : t("新建条目")}
                </button>
              )}
              {def && def.kind === "form" && editingId && (
                <button className="skill-btn" disabled={!!busy}
                        onClick={resetEntryForm}>{t("取消修改")}</button>
              )}
              {/* 微信也有这一对（用户「停止启动都做」）。
                  以前 `def.kind !== "wechat"` 把它整条排除掉 —— 结果是
                  停掉之后只能去扫码卡片才能再起来（「重新扫码登录」听着像
                  要重扫，其实有缓存凭据时只是把 `run()` 拉起来）。
                  判据没变：没登录/没配置不许点，标题说清要先做什么。 */}
              {def && def.kind !== "cli" && (
                running[openId]
                  ? <button className="skill-btn" disabled={!!busy}
                            onClick={() => stopChannel(openId)}>{t("停止通道")}</button>
                  : <button className="skill-btn"
                            disabled={!!busy || !(openRow && openRow.configured)}
                            title={openRow && openRow.configured ? ""
                                   : (def.kind === "wechat"
                                      ? t("先扫码登录") : t("先保存配置"))}
                            onClick={startChannel}>{t("启动通道")}</button>
              )}
              <button className="skill-btn" disabled={!!busy}
                      onClick={setDefault}>{t("设为默认")}</button>
              {busy && <span className="ch-note">{t("处理中…")}</span>}
            </div>

            {def && def.kind === "wechat" && (
              <p className="ch-note">
                {t("扫码成功后通道已在后台运行，不需要重启框架。")}
              </p>
            )}
          </section>
        </div>
      )}
    </section>
  );
}
