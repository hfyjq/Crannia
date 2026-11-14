import React, { useEffect, useState } from "react";
import { X } from "lucide-react";
import { jsonRequest } from "./api.js";
import { blankConfig } from "./endpoint-form.js";
import {
  ONBOARD_STEPS, SKIP_COOLDOWN_MS, defaultModelLabel,
  hasModelConfigs, modelBody, modelStepVerdict,
  skipButtonState, skipClick, stepIndex, workspaceBody, workspaceVerdict,
} from "./onboarding-model.js";
import {
  SELECTABLE_CHANNELS, channelById, channelFieldsValid, channelIconUrl, channelNotice,
  channelSaveBody, setDefaultBody, startBody,
} from "./channels-model.js";
import { addressHint, iconUrl } from "./provider-icons.js";
import { WechatLogin } from "./WechatLogin.jsx";

// 首启引导（从旧 WebUI 的 `base.html:431-852` 搬进 GUI）。
//
// 五步：欢迎 → 工作区 → 模型 API → 交流平台 → 完成。
//
// 判据在 `onboarding-model.js`（纯函数，node 逐条断言）—— 这里只管画。
//
// 复用后端的「首次」标记：`data/.onboarding-done`
//    （`GET /api/onboarding/status` / `POST /api/onboarding/complete`）。
//    老 WebUI 和新 GUI 共用同一个"第一次"，不会各弹一次。
//
// 模型那一步建的是「配置条目」（`POST /api/model-configs/create`）——
//    不是旧 WebUI 的 `.env` / `config.yaml` 那条老路（那条现在只剩兜底用途）。
//    第一条配置会自动成为默认；新开 / 未冻结的对话立刻用它，
//    已经在跑的对话保持原样（`scheduler._resolve_turn_model` 每轮读默认）。
//
// 全程不调 `/api/restart` —— 它会重置所有通道，还可能让微信
//    登录线程变成两份（见 `WechatLogin.jsx` 抬头）。

import { t } from "./i18n.js";
export function Onboarding({
  client, connected, notify, logo, version,
  replay = false, onDone, onClose,
}) {
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const [workspace, setWorkspace] = useState("");
  const [workspaceSaved, setWorkspaceSaved] = useState("");

  const [modelCards, setModelCards] = useState(null);
  const [modelLimits, setModelLimits] = useState({});
  //:进来一张卡都不选（用户「默认一个都没点，下面留空」）。
  const [modelForm, setModelForm] = useState(blankConfig(""));
  const [modelCreated, setModelCreated] = useState(null);
  const [modelSkipped, setModelSkipped] = useState(false);
  const [modelReused, setModelReused] = useState("");

  const [platform, setPlatform] = useState("wechat_channel");
  const [channelCfg, setChannelCfg] = useState(null);
  const [channelForms, setChannelForms] = useState({});
  const [wechatConfirmed, setWechatConfirmed] = useState(false);
  const [platformSaved, setPlatformSaved] = useState(false);

  const [skipArmed, setSkipArmed] = useState(false);
  const [skipRemaining, setSkipRemaining] = useState(0);

  function go(i) {
    setError("");
    setStep(i);
  }

  function toast(msg) {
    if (typeof notify === "function" && msg) notify(msg);
  }

  // ── 工作区：进入时预填（只读一次，失败就算了）─────────────────────
  useEffect(() => {
    if (step !== stepIndex("workspace") || !client || !connected) return;
    let alive = true;
    (async () => {
      try {
        //:要的是绝对路径 —— `settings.workspace` 可能写着相对名
        //:   （`workspace`），那对用户不是地址。`/api/workspaces` 的 `default`
        //:   来自 `core.workspace.default_output_dir()`（三层回退的唯一一处）。
        const s = await client.call("/api/workspaces");
        if (!alive) return;
        const prim = ((s && s.workspaces) || []).find((x) => x && x.primary) || {};
        const w = String((s && s.default) || prim.path || "");
        if (w) setWorkspace((x) => x || w);
      } catch (e) { /* 预填失败：用户自己填 */ }
    })();
    return () => { alive = false; };
  }, [step, client, connected]);

  // ── 模型：进入时读卡片表与上限 ───────────────────────────────────
  useEffect(() => {
    if (step !== stepIndex("model") || !client || !connected) return;
    if (modelCards !== null) return;
    let alive = true;
    setBusy("models");
    (async () => {
      try {
        const d = await client.call("/api/model-configs");
        if (!alive) return;
        const cards = (d && d.cards) || [];
        setModelCards(cards);
        setModelLimits({
          maxPerCard: (d && d.max_per_card) || 10,
          maxName: (d && d.max_name) || 40,
          maxModel: (d && d.max_model) || 120,
          maxUrl: (d && d.max_url) || 300,
          windowMin: (d && d.window_min) || 1024,
          windowMax: (d && d.window_max) || 10000000,
        });
        //:不预选、不预填 —— 空表就空着（见上面 `useState` 那条）。
      } catch (e) {
        if (alive) setError(e.message);
      } finally {
        if (alive) setBusy("");
      }
    })();
    return () => { alive = false; };
  }, [step, client, connected, modelCards]);

  // ── 平台：进入时读通道表 ─────────────────────────────────────────
  useEffect(() => {
    if (step !== stepIndex("platform") || !client || !connected) return;
    let alive = true;
    (async () => {
      try {
        const d = await client.call("/api/config");
        if (alive) setChannelCfg(d || null);
      } catch (e) {
        if (alive) setError(e.message);
      }
    })();
    return () => { alive = false; };
  }, [step, client, connected]);

  // ── 「暂不配置」的 3 秒冷却 ──────────────────────────────────────
  useEffect(() => {
    if (!skipArmed || skipRemaining <= 0) return;
    const timer = setTimeout(() => setSkipRemaining((x) => Math.max(0, x - 250)), 250);
    return () => clearTimeout(timer);
  }, [skipArmed, skipRemaining]);

  const cards = modelCards || [];
  const limits = modelLimits;
  const modelVerdict = modelStepVerdict(modelForm, limits);
  //:没选卡片时不借「其他」那张表的提示 —— 那会把 `gpt-4o` 写进占位符，
  //:   看起来像"已经替你填了"。空就是空。
  const addrHint = modelForm.card
    ? addressHint(modelForm.card)
    : { model: "", example: "", note: "" };
  const skipState = skipButtonState(skipArmed, skipRemaining);

  const chRow = (id) => ((channelCfg && channelCfg.channels) || [])
    .find((r) => r && r.id === id) || null;
  const chForm = channelForms[platform] || {};
  const chValid = channelFieldsValid(platform, chForm, !!(chRow(platform) && chRow(platform).configured));
  const platformDef = channelById(platform);

  // ── 工作区下一步 ─────────────────────────────────────────────────
  async function saveWorkspace(force = false) {
    const v = workspaceVerdict(workspace);
    if (!v.ok && !force) { setError(v.error); return; }
    if (v.ok) {
      setBusy("workspace");
      try {
        const r = await client.call("/api/onboarding/workspace",
                                    jsonRequest("POST", workspaceBody(v.value)));
        if (r && r.ok === false) throw new Error(r.error || t("保存失败"));
        setWorkspaceSaved((r && r.workspace) || v.value);
      } catch (e) {
        setError(e.message + t("（可以点「仍然继续」）"));
        setBusy("");
        return;
      }
      setBusy("");
    }
    go(stepIndex("model"));
  }

  async function browse() {
    try {
      const r = await client.call("/api/browse-folder", jsonRequest("POST", {}));
      if (r && r.path) { setWorkspace(r.path); setError(""); }
    } catch (e) { setError(e.message + t("（可以直接手动填写路径）")); }
  }

  // ── 模型 ─────────────────────────────────────────────────────────
  function chooseCard(id) {
    //:只换卡片；模型名不预填（占位符里有范例）。
    setModelForm(blankConfig(id));
  }

  async function saveModel() {
    const body = modelBody(modelForm, limits);
    if (!body) { setError(modelVerdict.error); return; }
    setBusy("model");
    try {
      const r = await client.call("/api/model-configs/create", jsonRequest("POST", body));
      if (r && r.ok === false) throw new Error(r.error || t("保存失败"));
      //:端点回的是 `{ok, entry, message}` —— 名字/模型在 `entry` 里。
      const e = (r && r.entry) || {};
      setModelCreated({ name: e.name || body.name || body.card,
                        model: e.model || body.model });
      setModelSkipped(false);
      setModelReused("");
      toast("模型配置已保存。");
      go(stepIndex("platform"));
    } catch (e) {
      setError(e.message);
    } finally { setBusy(""); }
  }

  function useExisting() {
    setModelCreated(null);
    setModelSkipped(false);
    setModelReused(defaultModelLabel(cards) || t("已配置"));
    go(stepIndex("platform"));
  }

  function skipModel() {
    const r = skipClick(skipArmed);
    if (!r.skipNow) {
      setSkipArmed(true);
      setSkipRemaining(SKIP_COOLDOWN_MS);
      return;
    }
    setModelSkipped(true);
    setModelCreated(null);
    setModelReused("");
    go(stepIndex("platform"));
  }

  // ── 平台 ─────────────────────────────────────────────────────────
  async function platformContinue(alsoStart = false) {
    if (platformDef && platformDef.kind === "wechat" && !wechatConfirmed) {
      setError(t("请先扫码登录，或者选择「稍后配置」。"));
      return;
    }
    if (platformDef && platformDef.kind === "form" && !chValid.ok && !platformSaved) {
      setError(chValid.error);
      return;
    }
    setBusy("platform");
    try {
      if (platformDef && platformDef.kind === "form" && chValid.ok) {
        const r = await client.call("/api/config", jsonRequest("POST", channelSaveBody(platform, chForm)));
        if (r && r.ok === false) throw new Error(r.error || t("保存失败"));
        setChannelForms((x) => ({ ...x, [platform]: {} }));
        setPlatformSaved(true);
      }
      // 微信：凭据由扫码流程自己落盘；这一条是 no-op，与旧 WebUI 同形。
      if (platformDef && platformDef.kind === "wechat") {
        await client.call("/api/config", jsonRequest("POST", channelSaveBody(platform, {})));
      }
      await client.call("/api/channel/set-default", jsonRequest("POST", setDefaultBody(platform)));
      if (alsoStart && platformDef && platformDef.kind === "form") {
        try {
          const r = await client.call("/api/channel/start", jsonRequest("POST", startBody(platform)));
          if (r && r.status === "already_running") toast("通道已经在运行。");
          else toast("通道已启动。");
        } catch (e) {
          //:框架没起来时后端回 400 —— 照实说，不许假装启动成功。
          setError(t("配置保存好了，但通道没有启动：") + e.message);
          setBusy("");
          return;
        }
      }
      go(stepIndex("done"));
    } catch (e) {
      setError(e.message);
    } finally { setBusy(""); }
  }

  // ── 完成 ─────────────────────────────────────────────────────────
  async function finish() {
    setBusy("done");
    try {
      const r = await client.call("/api/onboarding/complete", jsonRequest("POST", {}));
      if (r && r.ok === false) throw new Error(r.error || t("标记失败"));
      if (typeof onDone === "function") onDone();
    } catch (e) {
      if (replay) {
        toast("完成标记没有更新，但不影响使用。");
        if (typeof onDone === "function") onDone();
      } else {
        setError(e.message);
      }
    } finally { setBusy(""); }
  }

  // ── 渲染 ─────────────────────────────────────────────────────────
  const sm = (k) => (k === step ? "onboard-step-dot onboard-step-dot-current"
    : k < step ? "onboard-step-dot onboard-step-dot-done" : "onboard-step-dot");

  return (
    <div className="onboard-overlay" role="dialog" aria-modal="true">
      <section className="onboard-panel">
        {replay && typeof onClose === "function" && (
          <button className="round onboard-close" onClick={onClose} title={t("关闭")}><X size={18} /></button>
        )}

        <div className="onboard-steps">
          {ONBOARD_STEPS.map((_, i) => <i key={i} className={sm(i)} />)}
        </div>

        {!connected && (
          <p className="onboard-offline">{t("本地服务未连接，部分步骤无法保存。")}</p>
        )}

        <div className="onboard-body">
          {step === stepIndex("welcome") && (
            <>
              {logo ? <img className="onboard-logo" src={logo} alt="Crannia" /> : null}
              <div className="onboard-version">Crannia{version ? ` v${version}` : ""}</div>
              {/* 开屏第一句 —— 用户逐字给的。 */}
              <h2 className="onboard-title">{t("这似乎是你的首次使用，让我们从这里开始")}</h2>
              <p className="onboard-sub">{t("模块化 AI Agent 框架")}</p>
              <p className="onboard-note">{t("让我们开始配置引导")}</p>
              <div className="onboard-actions">
                <button className="onboard-btn onboard-btn-primary"
                        onClick={() => go(stepIndex("workspace"))}>{t("开始配置")}</button>
              </div>
            </>
          )}

          {step === stepIndex("workspace") && (
            <>
              <h2 className="onboard-title">{t("配置工作区")}</h2>
              <p className="onboard-sub">{t("Agent 将在这里开始工作")}</p>
              <div className="onboard-input-row">
                <input className="onboard-input" value={workspace}
                       placeholder={t("点击右侧浏览，或直接填写目录路径…")}
                       onChange={(e) => setWorkspace(e.target.value)} />
                <button className="onboard-btn onboard-btn-browse"
                        onClick={browse} disabled={!connected}>{t("浏览")}</button>
              </div>
              <p className="onboard-note">{t("后续可以在设置里修改。")}</p>
              {error && <p className="onboard-error">{error}</p>}
              <div className="onboard-actions">
                <button className="onboard-btn onboard-btn-ghost"
                        onClick={() => go(stepIndex("welcome"))}>{t("返回")}</button>
                <button className="onboard-btn onboard-btn-primary"
                        disabled={!!busy || !connected} onClick={() => saveWorkspace()}>
                  {t("下一步")}
                </button>
                {error && (
                  <button className="onboard-btn onboard-btn-ghost" disabled={!connected}
                          onClick={() => saveWorkspace(true)}>{t("仍然继续")}</button>
                )}
              </div>
            </>
          )}

          {step === stepIndex("model") && (
            <>
              <h2 className="onboard-title">{t("配置模型 API")}</h2>
              <p className="onboard-sub">{t("选择厂商，填模型名与 API Key")}</p>

              {hasModelConfigs(cards) && (
                <div className="onboard-note">
                  {t("已有模型配置：")}<b>{defaultModelLabel(cards) || t("（已有一条）")}</b>
                  <button className="onboard-btn onboard-btn-ghost"
                          onClick={useExisting}>{t("沿用现有配置，下一步")}</button>
                </div>
              )}

              {modelCards === null && <p className="onboard-note">{t("读取配置表…")}</p>}

              <div className="onboard-provider-grid">
                {cards.map((c) => {
                  const ic = iconUrl(client, c.id);
                  return (
                    <button key={c.id}
                            className={"onboard-provider-chip"
                              + (modelForm.card === c.id ? " onboard-provider-chip-selected" : "")}
                            onClick={() => chooseCard(c.id)}>
                      {ic ? <img src={ic} alt="" /> : null}
                      <span>{c.label}</span>
                    </button>
                  );
                })}
              </div>

              <div className="onboard-config-form">
                <label className="field"><span>{t("配置名称（可留空）")}</span>
                  <input value={modelForm.name} maxLength={limits.maxName}
                         placeholder={(cards.find((c) => c.id === modelForm.card) || {}).label || ""}
                         onChange={(e) => setModelForm((f) => ({ ...f, name: e.target.value }))} /></label>
                <label className="field"><span>{t("模型名")}</span>
                  <input value={modelForm.model} maxLength={limits.maxModel}
                         placeholder={addrHint.model}
                         onChange={(e) => setModelForm((f) => ({ ...f, model: e.target.value }))} /></label>
                <label className="field"><span>{t("请求地址（可留空）")}</span>
                  <input value={modelForm.base_url} maxLength={limits.maxUrl}
                         placeholder={addrHint.example}
                         onChange={(e) => setModelForm((f) => ({ ...f, base_url: e.target.value }))} /></label>
                {addrHint.note ? <p className="onboard-note">{t(addrHint.note)}</p> : null}
                <label className="field"><span>API Key</span>
                  <input type="password" value={modelForm.api}
                         placeholder={t("粘贴 API Key…")}
                         onChange={(e) => setModelForm((f) => ({ ...f, api: e.target.value }))} /></label>
              </div>

              {!modelForm.card && (
                <p className="onboard-note">{t("先选一个厂商卡，再填模型名与 API Key。")}</p>
              )}
              {skipArmed && (
                <p className="onboard-warn">{t("后续可在端配置里补充模型，以获得完整体验。")}</p>
              )}
              {error && <p className="onboard-error">{error}</p>}

              <div className="onboard-actions">
                <button className="onboard-btn onboard-btn-ghost"
                        onClick={() => go(stepIndex("workspace"))}>{t("返回")}</button>
                <button className="onboard-btn onboard-btn-ghost"
                        disabled={skipState.disabled || !connected}
                        onClick={skipModel}>{skipState.text}</button>
                <button className="onboard-btn onboard-btn-primary"
                        disabled={!!busy || !connected || !modelForm.card || !modelVerdict.ok}
                        title={!modelForm.card ? t("先选一个厂商卡") : (modelVerdict.error || "")}
                        onClick={saveModel}>{t("保存并下一步")}</button>
              </div>
            </>
          )}

          {step === stepIndex("platform") && (
            <>
              <h2 className="onboard-title">{t("选择交流平台")}</h2>
              <p className="onboard-sub">{t("选择你和 Agent 交流的地方")}</p>

              <div className="onboard-platform-grid">
                {SELECTABLE_CHANNELS.map((d) => {
                  const row = chRow(d.id);
                  const ic = channelIconUrl(client, d.id);
                  return (
                    <button key={d.id}
                            className={"onboard-platform-chip"
                              + (platform === d.id ? " onboard-platform-chip-selected" : "")}
                            onClick={() => { setPlatform(d.id); setPlatformSaved(false); setError(""); }}>
                      {ic ? <img src={ic} alt="" /> : <span aria-hidden="true">🖥</span>}
                      <span>{t(d.label)}</span>
                      <small>{row && row.configured ? t("已配置") : (d.kind === "cli" ? t("始终可用") : t("未配置"))}</small>
                    </button>
                  );
                })}
              </div>

              {/* 微信那一支居中（用户「这里几个按钮居中一下」）；
                  表单类的字段仍左对齐，不然标签和输入框会错位。 */}
              <div className={"onboard-config-form"
                    + (platformDef && platformDef.kind === "wechat" ? " onboard-platform-wx" : "")}>
                {platformDef && platformDef.kind === "wechat" && (
                  <WechatLogin client={client} connected={connected}
                               configured={!!(chRow(platform) && chRow(platform).configured)}
                               onConfirmed={() => setWechatConfirmed(true)}
                               onRestart={() => setWechatConfirmed(false)} />
                )}
                {platformDef && platformDef.kind === "form" && (
                  <>
                    {/* 与通道配置页同一句话：能力没完工的通道，接入前先讲清楚。 */}
                    {channelNotice(platform) ? (
                      <p className="onboard-note">{channelNotice(platform).body}</p>
                    ) : null}
                    {(platformDef.fields || []).map((f) => (
                      <label className="field" key={f.key}>
                        <span>{t(f.label)}</span>
                        <input type={f.secret ? "password" : "text"}
                               value={chForm[f.key] == null ? "" : chForm[f.key]}
                               placeholder={chRow(platform) && chRow(platform).configured ? t("留空 = 不改") : t("输入 {f.label}…", { "f.label": t(f.label) })}
                               onChange={(e) => setChannelForms((x) => ({
                                 ...x, [platform]: { ...(x[platform] || {}), [f.key]: e.target.value },
                               }))} />
                      </label>
                    ))}
                    {!chValid.ok && <p className="onboard-note">{chValid.error}</p>}
                  </>
                )}
              </div>

              {error && <p className="onboard-error">{error}</p>}
              <div className="onboard-actions">
                <button className="onboard-btn onboard-btn-ghost"
                        onClick={() => go(stepIndex("model"))}>{t("返回")}</button>
                <button className="onboard-btn onboard-btn-ghost"
                        onClick={() => go(stepIndex("done"))}>{t("稍后配置")}</button>
                {platformDef && platformDef.kind === "form" && (
                  <button className="onboard-btn onboard-btn-ghost"
                          disabled={!!busy || !connected || !chValid.ok}
                          onClick={() => platformContinue(true)}>{t("保存并启动")}</button>
                )}
                <button className="onboard-btn onboard-btn-primary"
                        disabled={!!busy || !connected} onClick={() => platformContinue(false)}>
                  {t("从这里开始")}
                </button>
              </div>
            </>
          )}

          {step === stepIndex("done") && (
            <>
              <h2 className="onboard-title">{t("配置完成")}</h2>
              <p className="onboard-sub">{t("可以开始使用 Crannia 了")}</p>

              <div className="onboard-summary">
                <div className="onboard-summary-row">
                  <span>{t("工作区")}</span><b>{workspaceSaved || workspace || t("未设置")}</b>
                </div>
                <div className="onboard-summary-row">
                  <span>{t("模型")}</span>
                  <b>{modelCreated ? `${modelCreated.name} · ${modelCreated.model}`
                    : modelReused ? t("沿用现有配置") + (modelReused === "已配置" ? "" : "：" + modelReused)
                    : modelSkipped ? t("暂未配置（已跳过）") : t("暂未配置")}</b>
                </div>
                <div className="onboard-summary-row">
                  <span>{t("平台")}</span>
                  <b>{platformDef ? t(platformDef.label) : t("未选择")}</b>
                </div>
              </div>

              <p className="onboard-note">
                {modelCreated
                  ? t("新开的对话会使用这条模型配置；已经在跑的对话保持原样。")
                  : t("之后可以在「端配置」里随时补充或更换模型与通道。")}
              </p>
              {error && <p className="onboard-error">{error}</p>}

              <div className="onboard-actions">
                <button className="onboard-btn onboard-btn-primary"
                        disabled={!!busy || !connected} onClick={finish}>
                  {t("进入 Crannia")}
                </button>
              </div>
            </>
          )}
        </div>

        <div className="onboard-steps-text">
          {step + 1} / {ONBOARD_STEPS.length}
        </div>
      </section>
    </div>
  );
}
