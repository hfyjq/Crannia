// 设置包的导出 / 导入。
//
// 判据全在后端（`core/settings_transfer.py` 的 docstring）。这一层只做三件事：
//   · 把差异一格格摆出来（`changed` 是 `{键: [现在, 包里]}` —— 与「恢复默认」同形）；
//   · 让用户逐项勾机器相关项（默认一项都不勾：`boot_on_startup` 会改注册表）；
//   · 口令只在这一层待过（后端立刻拿去解包，不落盘、不记日志）。
//
// 四个外观键（`lv.colors` / `lv.colors.dark` / `lv.theme` / `lv.sideW`）存在
//    GUI 自己的 origin 里，后端读不到，导出时前端塞进包的 `gui` 段，
//    导入时前端写回。三个「已看过」标记不带（那是知情状态，带过去等于替新机器点掉安全提示）。
import React, { useMemo, useRef, useState } from "react";
import { jsonRequest } from "./api.js";
import { t } from "./i18n.js";

//:与 `设置项勘察-.md` §2 那张表逐字一致（只带四个外观键）。
const GUI_KEYS = ["lv.colors", "lv.colors.dark", "lv.theme", "lv.sideW"];

function readGuiKeys() {
  const out = {};
  for (const k of GUI_KEYS) {
    try {
      const v = localStorage.getItem(k);
      if (v != null) out[k] = v;
    } catch { /* 隐私模式：读不到就不带，别让它把导出整个弄挂 */ }
  }
  return out;
}

function writeGuiKeys(obj) {
  if (!obj || typeof obj !== "object") return 0;
  let n = 0;
  for (const k of GUI_KEYS) {
    if (typeof obj[k] !== "string") continue;
    try { localStorage.setItem(k, obj[k]); n += 1; } catch { /* 同上 */ }
  }
  return n;
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function shown(v) {
  if (v === null || v === undefined) return t("（没有）");
  if (typeof v === "string") return v;
  try { return JSON.stringify(v); } catch { return String(v); }
}

/** 键名后面那句话。只给会让人停下来想一秒的那几格写说明。 */
const KEY_NOTES = {
  auto_approve: "会自动批准跨区访问",
  relax_command_tier: "危险命令降级成普通档",
  full_access_unlock: "完全权限",
  workspace: "本机路径，不随包出",
  web_token: "登录令牌，只在「含凭据」时加密携带",
  interface_language: "空 = 跟随系统",
  framework_preset: "预设名",
};

export function SettingsTransfer({ client, notify, mode, onClose, onReload }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // ── 导出那一侧 ──
  const [withCreds, setWithCreds] = useState(false);
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");

  // ── 导入那一侧 ──
  const fileRef = useRef(null);
  const [fileName, setFileName] = useState("");
  const [bundle, setBundle] = useState(null);
  const [plan, setPlan] = useState(null);
  const [picked, setPicked] = useState({});        // {键: true} —— 机器项
  const [pwImport, setPwImport] = useState("");
  const [withCredsImport, setWithCredsImport] = useState(false);
  const [withAllowlist, setWithAllowlist] = useState(true);
  const [withModelConfigs, setWithModelConfigs] = useState(true);

  const changedRows = useMemo(() => {
    const c = (plan && plan.changed) || {};
    return Object.keys(c).sort().map((k) => [k, c[k]]);
  }, [plan]);

  const machineRows = useMemo(() => {
    const m = (plan && plan.machine) || {};
    return Object.keys(m).sort().map((k) => [k, m[k]]);
  }, [plan]);

  async function doExport() {
    setErr("");
    if (withCreds) {
      if (!password) { setErr(t("携带凭据已勾选，请先设置口令")); return; }
      if (password !== password2) { setErr(t("两次输入不一致")); return; }
    }
    setBusy(true);
    try {
      const d = await client.call("/api/settings/export", jsonRequest("POST", {
        include_credentials: withCreds,
        password: withCreds ? password : "",
        gui: readGuiKeys(),
      }));
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
      saveBlob(new Blob([JSON.stringify(d.bundle, null, 2)],
                        { type: "application/json" }),
               `crannia-settings-${stamp}.json`);
      notify(t("已导出设置包。"));
      onClose();
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  async function pickFile(e) {
    setErr("");
    setPlan(null);
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    setFileName(f.name);
    try {
      const text = await f.text();
      const b = JSON.parse(text);
      setBundle(b);
      await preview(b, { password: pwImport, withCreds: withCredsImport,
                         picked });
    } catch (e) {
      setBundle(null);
      setErr(t("这不是一个设置包（读不出来）。"));
    }
  }

  async function preview(b, opts = {}) {
    if (!b) return;
    setBusy(true);
    setErr("");
    try {
      const d = await client.call("/api/settings/import", jsonRequest("POST", {
        bundle: b,
        dry_run: true,
        password: opts.password !== undefined ? opts.password : pwImport,
        with_credentials: opts.withCreds !== undefined ? opts.withCreds : withCredsImport,
        selections: Object.keys(opts.picked || picked).filter((k) => picked[k]),
        with_allowlist: withAllowlist,
        with_model_configs: withModelConfigs,
      }));
      setPlan(d);
    } catch (e) { setErr(e.message); setPlan(null); }
    finally { setBusy(false); }
  }

  async function applyImport() {
    if (!bundle) return;
    setBusy(true);
    setErr("");
    try {
      const d = await client.call("/api/settings/import", jsonRequest("POST", {
        bundle,
        dry_run: false,
        password: pwImport,
        with_credentials: withCredsImport,
        selections: Object.keys(picked).filter((k) => picked[k]),
        with_allowlist: withAllowlist,
        with_model_configs: withModelConfigs,
      }));
      const n = writeGuiKeys(d.gui);
      notify(n ? t("已导入设置（包含 {n} 格外观）", { n }) : t("已导入设置。"));
      await onReload();
      onClose();
      //: 界面语言改了要整页重载，与设置页保存那条是同一个判据
      //:   （`App.jsx` 里紧跟着注释）：模块级词表（`NAV` / `POOLS` / 设置表）
      //:   在 import 时就定下来了，只 `onReload()` 会「设置里已经是中文、
      //:   界面还是英文」—— 正是 `B13` 那个形状：同一件事两个入口只落了一处。
      if (d && d.changed
          && Object.prototype.hasOwnProperty.call(d.changed, "interface_language")) {
        window.location.reload();
      }
    } catch (e) { setErr(e.message); }
    finally { setBusy(false); }
  }

  const creds = (plan && plan.credentials) || {};

  return (
    <div className="backdrop" onClick={busy ? undefined : onClose}>
      <section className="xfer" role="dialog" aria-modal="true"
               onClick={(e) => e.stopPropagation()}>
        <div className="safety-head">
          <h2 className="safety-title">
            {mode === "export" ? t("导出设置") : t("导入设置")}
          </h2>
        </div>

        {mode === "export" ? (
          <>
            <p className="safety-body">
              {t("导出内容：偏好设置 · 白名单 · 模型配置（不含 api key）· 界面外观")}
            </p>
            <p className="xfer-muted">
              {t("请注意工作区路径、工作组、监听器、规避目录不会进包")}
            </p>
            <label className="xfer-line">
              <input type="checkbox" checked={withCreds} disabled={busy}
                     onChange={(e) => setWithCreds(e.target.checked)} />
              {t("连凭据一起导出（口令加密）")}
            </label>
            {withCreds ? (
              <div className="xfer-pw">
                <p className="xfer-warn">
                  {t("口令丢了不会有任何找回通道 —— 务必妥善保存")}
                </p>
                <input type="password" value={password} disabled={busy}
                       placeholder={t("口令")} autoComplete="new-password"
                       onChange={(e) => setPassword(e.target.value)} />
                <input type="password" value={password2} disabled={busy}
                       placeholder={t("重复输入口令")} autoComplete="new-password"
                       onChange={(e) => setPassword2(e.target.value)} />
              </div>
            ) : null}
          </>
        ) : (
          <>
            <p className="safety-body">
              {t("选择导入包体")}
            </p>
            {/* 原生 file 控件的按钮文字由 Chromium 按系统语言渲染，
                `t()` 管不到 —— 英文界面下它照样是「选择文件 / 未选择任何文件」。
                藏掉它，自己画一个按钮 + 文件名，与其余文案同一条语言轴。 */}
            <input ref={fileRef} type="file" accept=".json,application/json"
                   className="xfer-file" disabled={busy}
                   onChange={pickFile} hidden />
            <div className="xfer-pick">
              <button type="button" disabled={busy}
                      onClick={() => { if (fileRef.current) fileRef.current.click(); }}>
                {t("选择文件")}
              </button>
              <span className="xfer-muted">{fileName || t("未选择任何文件")}</span>
            </div>

            {plan ? (
              <div className="xfer-plan">
                <p className="xfer-muted">
                  {t("来自 {v}（导出于 {at}）。", {
                    v: (plan.summary && plan.summary.app_version) || t("未知版本"),
                    at: (plan.summary && plan.summary.exported_at) || "?",
                  })}
                </p>

                <h3>{t("会变更设置")}</h3>
                {changedRows.length === 0 ? (
                  <p className="xfer-muted">{t("没有一格会变。")}</p>
                ) : (
                  <table className="xfer-table">
                    <thead><tr>
                      <th>{t("键")}</th><th>{t("现在")}</th><th>{t("包里")}</th>
                    </tr></thead>
                    <tbody>
                      {changedRows.map(([k, pair]) => (
                        <tr key={k} className={KEY_NOTES[k] ? "hot" : ""}>
                          <td><code>{k}</code>
                            {KEY_NOTES[k] ? <em>{t(KEY_NOTES[k])}</em> : null}</td>
                          <td>{shown(pair[0])}</td>
                          <td>{shown(pair[1])}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}

                {machineRows.length ? (
                  <>
                    <h3>{t("本机相关设置（默认不导入）")}</h3>
                    <p className="xfer-muted">
                      {t("需要条目请勾选")}
                    </p>
                    {machineRows.map(([k, pair]) => (
                      <label className="xfer-line" key={k}>
                        <input type="checkbox" checked={!!picked[k]} disabled={busy}
                               onChange={(e) => {
                                 const next = { ...picked, [k]: e.target.checked };
                                 setPicked(next);
                                 preview(bundle, { picked: next });
                               }} />
                        <code>{k}</code>
                        <span className="xfer-muted">
                          {shown(pair[0])} → {shown(pair[1])}
                        </span>
                      </label>
                    ))}
                  </>
                ) : null}

                <h3>{t("其他部分")}</h3>
                <label className="xfer-line">
                  <input type="checkbox" checked={withAllowlist} disabled={busy}
                         onChange={(e) => setWithAllowlist(e.target.checked)} />
                  {t("网络与命令白名单（{n} 条）", {
                    n: (plan.summary && plan.summary.allowlist_items) || 0 })}
                </label>
                <label className="xfer-line">
                  <input type="checkbox" checked={withModelConfigs} disabled={busy}
                         onChange={(e) => setWithModelConfigs(e.target.checked)} />
                  {t("模型配置（{n} 条，不含 api key；只做合并）", {
                    n: (plan.summary && plan.summary.model_configs) || 0 })}
                </label>

                {creds.present ? (
                  <div className="xfer-pw">
                    <label className="xfer-line">
                      <input type="checkbox" checked={withCredsImport} disabled={busy}
                             onChange={(e) => {
                               setWithCredsImport(e.target.checked);
                               preview(bundle, { withCreds: e.target.checked });
                             }} />
                      {t("导入凭据（{n} 项）", { n: (creds.names || []).length })}
                    </label>
                    <p className="xfer-muted">{t("下为字段清单")}</p>
                    <p className="xfer-muted">{(creds.names || []).join(" · ")}</p>
                    {withCredsImport ? (
                      <input type="password" value={pwImport} disabled={busy}
                             placeholder={t("在此处输入口令")} autoComplete="off"
                             onChange={(e) => setPwImport(e.target.value)}
                             onBlur={() => preview(bundle, { password: pwImport,
                                                             withCreds: true })} />
                    ) : null}
                    {creds.error ? <p className="xfer-warn">{creds.error}</p> : null}
                  </div>
                ) : null}

                {(plan.warnings || []).length ? (
                  <ul className="xfer-warn">
                    {plan.warnings.map((w, i) => <li key={i}>{w}</li>)}
                  </ul>
                ) : null}
              </div>
            ) : null}
          </>
        )}

        {err ? <p className="xfer-warn">{err}</p> : null}

        <div className="safety-actions">
          {mode === "export" ? (
            <button className="safety-go" disabled={busy} onClick={doExport}>
              {t("导出")}
            </button>
          ) : (
            <button className="safety-go" disabled={busy || !plan}
                    onClick={applyImport}>
              {t("导入")}
            </button>
          )}
          <button className="skill-btn" disabled={busy} onClick={onClose}>
            {t("取消")}
          </button>
        </div>
      </section>
    </div>
  );
}
