import React, { useEffect, useState } from "react";
import { Check, Pencil, Plug, RefreshCw, Star, Trash2, X } from "lucide-react";
import { jsonRequest } from "./api.js";
import {
  apiHint, blankConfig, buildCreatePayload, buildUpdatePayload, canAdd,
  cardSubtitle, entryToForm, testVerdict, validateConfig,
} from "./endpoint-form.js";
import { addressHint, iconUrl } from "./provider-icons.js";
import { Channels } from "./Channels.jsx";

// 「端配置」那一屏 —— 配置条目：用户自己建"用什么模型"。
//
//   形态：12 张大类卡片（GPT / Claude / Deepseek / Qwen / GLM / Kimi / Gemini /
//   Grok / Minimax / 火山引擎 / 腾讯混元 / 其他），点一张开「管理此配置」浮窗：
//   上 = 该类已配置的列表，下 = 新建（配置名称 / 模型名 / 请求地址 / api）。
//   「高级选项」勾开才出现上下文容量那一格，上面一条红横幅。
//
// 判据全在 `endpoint-form.js`（纯函数，node 逐条断言）—— 这里只管画。
//    所有上限由后端给（`GET /api/model-configs`），一个都不在这里写死。
//
// 「设为默认」不谎报当场生效：它落的是一条账，而正在跑的那个 provider
//    在框架下一次启动时才换（`PipelineScheduler.initialize()` 里接的）。
//    这一屏把这件事印在脸上（见下面那行 `.ep-note`）—— 用户按了没变
//    而界面不说，就是界面在说谎。
//
// 卡片 logo：有厂商图标的卡片显示厂商图标；「其他」没有共同商标，用自绘字形。

/** 「其他」那张卡的自绘字形（一个中转/接驳的意象）。
 *
 *  12 张卡里只有它没有厂商 logo，而且这是有意的 ——
 *  中转站没有共同的商标，拿谁的去代表都是指错人。
 *  自绘 = 没有第三方素材，也就没有署名义务。
 */
/** 覆盖档 → 一句人话（`title` 用）。三档的措辞与后端语义逐字对上。 */
import { t } from "./i18n.js";
import { Confirm } from "./Confirm.jsx";
function capModeNote(modes) {
  const m = modes || {};
  const one = (v, label) => (v === "auto" ? t("{label}自动判定", { "label": label })
    : v === "on" ? t("{label}强制开启", { "label": label }) : t("{label}强制关闭", { "label": label }));
  return `${one(m.vision, t("图片"))} · ${one(m.audio, t("音频"))}`;
}

/** 三态那一格 —— 用户自己的能力覆盖口。 */
function CapModeSelect({ label, value, onChange, disabled }) {
  return (
    <label className="field"><span>{label}</span>
      <select value={value || "auto"} disabled={disabled}
              onChange={(e) => onChange(e.target.value)}>
        <option value="auto">{t("自动判定（按模型名）")}</option>
        <option value="on">{t("强制开启（这个模型能收）")}</option>
        <option value="off">{t("强制关闭（这个模型不能收）")}</option>
      </select></label>
  );
}

function RelayGlyph() {
  return (
    <svg viewBox="0 0 24 24" width="1em" height="1em" aria-hidden="true">
      <circle cx="12" cy="12" r="3.1" fill="none" stroke="currentColor" strokeWidth="1.7" />
      <path d="M12 2.6v5.3M12 16.1v5.3M2.6 12h5.3M16.1 12h5.3"
            stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

export function Endpoint({ client, notify, connected }) {
  //: `null` = 还没读到（与"读到了、一个条目都没有"是两件事）
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  //:打开的是哪张卡（`null` = 浮窗关着）。12 张卡共用同一个浮窗，
  //:   不各造一个 —— 同 `Groups` 那条（一份表单，不是十二份）。
  const [openCard, setOpenCard] = useState(null);
  const [form, setForm] = useState(blankConfig);
  //: 正在编辑哪一条（`null` = 下面那半是"新建"）。
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);
  //:删除要二次确认（用户）：不可逆的动作一次点击不许发生。
  const [confirmDel, setConfirmDel] = useState(null);
  //:联通测试的结果 —— 正文可能有好几行，所以是块内一行，不是 toast。
  const [probe, setProbe] = useState(null);

  const limits = {
    maxPerCard: (data && data.max_per_card) || 10,
    maxName: (data && data.max_name) || 40,
    maxModel: (data && data.max_model) || 120,
    maxUrl: (data && data.max_url) || 300,
    windowMin: (data && data.window_min) || 1024,
    windowMax: (data && data.window_max) || 10000000,
  };

  async function load() {
    try {
      const d = await client.call("/api/model-configs");
      setData(d);
      setErr(d.error || "");
    } catch (e) {
      setData({ cards: [], error: e.message });
      setErr(e.message);
    }
  }
  useEffect(() => { load(); }, [client]);   // eslint-disable-line

  const cards = (data && data.cards) || [];
  const card = cards.find((c) => c.id === openCard) || null;
  const room = canAdd(card, limits.maxPerCard);
  const verdict = validateConfig(form, limits);
  //:地址提示跟着表单上的卡片走（新建时是 `blankConfig(card.id)`、
  //:   编辑时是 `entryToForm(...)` 带回来的那个）—— 两处都不用各自再算一遍。
  const addrHint = addressHint(form.card);

  function open_ (c) {
    setOpenCard(c.id);
    setEditing(null);
    setForm(blankConfig(c.id));
    setProbe(null);
  }
  function close() {
    setOpenCard(null);
    setEditing(null);
    setProbe(null);
  }

  async function act(path, body, okMsg) {
    setBusy(true);
    try {
      const r = await client.call(path, jsonRequest("POST", body));
      notify((r && r.message) || okMsg);
      await load();
      return r;
    } catch (e) {
      notify(e.message);
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (editing) {
      const body = buildUpdatePayload(editing.id, form, limits);
      if (!body) return;                 // 按钮本来就是灰的；这是第二道
      const r = await act("/api/model-configs/update", body, t("已保存。"));
      if (r) { setEditing(null); setForm(blankConfig(openCard)); }
    } else {
      const body = buildCreatePayload(form, limits);
      if (!body) return;
      const r = await act("/api/model-configs/create", body, t("配置已保存。"));
      if (r) setForm(blankConfig(openCard));
    }
  }

  async function probeOne(payload, okMsg) {
    setBusy(true);
    setProbe({ pending: true, text: t("正在发一次最小请求…") });
    try {
      const r = await client.call("/api/model-configs/test",
                                  jsonRequest("POST", payload));
      setProbe({ ...testVerdict(r), scope_note: (r && r.scope_note) || "" });
    } catch (e) {
      setProbe({ ok: false, text: e.message });
      notify(okMsg || e.message);
    } finally {
      setBusy(false);
    }
  }

  if (!connected) return <p className="muted">{t("启动本地服务后查看端配置。")}</p>;

  return (
    <section className="page">
      <div className="page-head">
        <div><small>CRANNIA</small><h1>{t("端配置")}</h1></div>
        <div className="head-actions">
          <button onClick={load} disabled={!connected}><RefreshCw size={15} />{t("刷新")}</button>
        </div>
      </div>

      <p className="muted">
        {t("在此处配置你的模型：")}<b>{t("配置名称 / 模型名 / 请求地址 / api")}</b>
        {t("，每张卡至多 {limits.maxPerCard} 条可用配置。", { "limits.maxPerCard": limits.maxPerCard })}
      </p>

      {data === null && <p className="muted">{t("读取中…")}</p>}
      {err && <p className="muted">{err}</p>}

      <div className="ep-cards">
        {cards.map((c, i) => {
          const logo = iconUrl(client, c.id);
          return (
            <button className={"ep-card" + (c.is_default_card ? " ep-card-default" : "")}
                    style={{ animationDelay: (i * 0.05) + "s" }}
                    key={c.id} onClick={() => open_(c)}>
              {/* 右上角那个深绿圆点 = 这张卡下有「默认启用」的那条 */}
              {c.is_default_card && <i className="ep-dot" title={t("这张卡下有默认启用的配置")} />}
              {/* 厂商图标：`web/static/img/provider-*.svg`（第三方素材的出处与许可
                  见 `THIRD-PARTY.md`）。
                  「其他」故意没有图标 —— 它是中转站的兜底，拿任何一家的 logo
                  去代表它都是指错人，走下面那个自绘字形。 */}
              <span className="ep-logo">
                {logo ? <img src={logo} alt="" /> : <RelayGlyph />}
              </span>
              <b>{c.label}</b>
              <span className="muted">{cardSubtitle(c, limits.maxPerCard)}</span>
            </button>
          );
        })}
      </div>

      {/* 通道卡（从旧 WebUI 的端配置页搬来）—— 放在模型配置之后，
          两段各有自己的小标题；判据在 `channels-model.js`。 */}
      <Channels client={client} notify={notify} connected={connected} />

      {card && (
        <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && close()}>
          <section className="ep-panel">
            <div className="settings-head">
              <div><small>{t("管理此配置")}</small><h2>{card.label}</h2></div>
              <button className="round" onClick={close}><X size={18} /></button>
            </div>

            {/* ── 上：这一类已经配好的 ─────────────────────────────── */}
            <div className="ep-list">
              {!card.configs.length && (
                <p className="muted">{t("「{card.label}」还没有配置 —— 在下面建一条。", { "card.label": card.label })}</p>
              )}
              {card.configs.map((row) => (
                <div className={"ep-row" + (row.is_default ? " ep-row-default" : "")} key={row.id}>
                  <div className="ep-row-main">
                    <b>{row.name}</b>
                    {row.is_default && <span className="ep-badge">{t("默认启用")}</span>}
                    <span className="muted">
                      {row.model} · {row.base_url || t("（用默认地址）")} · {t(apiHint(row))}
                      {row.window ? " · " + t("窗口 {w}", { w: row.window }) : ""}
                    </span>
                    {/* 能力跟模型走 —— 只读展示，
                        值由后端按 (provider, model) 查表算出；来源放进 title，
                        不替用户做"填错就关掉功能"的决定（一处定义）。 */}
                    {row.capabilities ? (
                      <span className="ep-caps"
                            title={t("来源：图片 {row.capabilities.vision_source || \"?\"} · 音频 {row.capabilities.audio_source || \"?\"}", { "row.capabilities.vision_source || \"?\"": row.capabilities.vision_source || "?", "row.capabilities.audio_source || \"?\"": row.capabilities.audio_source || "?" })}>
                        {row.capabilities.vision ? <span>{t("图片")}</span> : null}
                        {row.capabilities.audio ? <span>{t("音频")}</span> : null}
                        {!row.capabilities.vision && !row.capabilities.audio
                          ? <span>{t("纯文本")}</span> : null}
                        {/* 用户自己勾过就标出来 —— 否则"强制关"在
                            chips 上长得跟"自动判定为没有"一模一样（界面在说谎）。 */}
                        {row.capability_modes
                         && (row.capability_modes.vision !== "auto"
                             || row.capability_modes.audio !== "auto") ? (
                          <span className="ep-caps-override" title={capModeNote(row.capability_modes)}>{t("手动")}</span>
                        ) : null}
                      </span>
                    ) : null}
                  </div>
                  <div className="ep-row-acts">
                    {!row.is_default && (
                      <button className="skill-btn" disabled={busy} title={t("新开的对话用它（框架重启后生效）")}
                              onClick={() => act("/api/model-configs/default", { id: row.id }, t("已设为默认。"))}>
                        <Star size={13} />{t("设为默认")}
                      </button>
                    )}
                    <button className="skill-btn" disabled={busy} title={t("测一次：真的发一个最小请求过去")}
                            onClick={() => probeOne({ id: row.id })}><Plug size={13} />{t("联通测试")}</button>
                    <button className="skill-btn" disabled={busy} title={t("改这条")}
                            onClick={() => { setEditing(row); setForm(entryToForm(row)); setProbe(null); }}>
                      <Pencil size={13} />{t("修改")}
                    </button>
                    <button className="skill-btn skill-btn-danger" disabled={busy || row.is_default}
                            title={row.is_default ? t("默认那条不能删 —— 先把另一条设为默认")
                                                  : t("删掉这条配置")}
                            onClick={() => setConfirmDel(row)}>
                      <Trash2 size={13} />{t("删除")}
                    </button>
                  </div>
                </div>
              ))}
            </div>

            {/* ── 下：新建 / 编辑（同一张表单）──────────────────── */}
            <div className="ep-form">
              <h3>{editing ? t("编辑「{editing.name}」", { "editing.name": editing.name }) : t("新建配置")}</h3>

              <label className="field"><span>{t("配置名称")}</span>
                <input value={form.name} maxLength={limits.maxName}
                       placeholder={card.label}
                       onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} /></label>

              {/* 模型名的范例跟着卡片变（见 `provider-icons.js::MODEL_EXAMPLE`）。
                  只是占位符：从不校验，用户填什么都发。 */}
              <label className="field"><span>{t("模型名")}</span>
                <input value={form.model} maxLength={limits.maxModel}
                       placeholder={addrHint.model}
                       onChange={(e) => setForm((f) => ({ ...f, model: e.target.value }))} /></label>

              {/* 请求地址那一格 —— 提示跟着卡片变：
                  Claude 卡「不要以 /v1 结尾」，其余卡「填到 API 根为止」。
                  只提示、不代改：自动增删 `/v1` 会把本来就对的自定义路径改坏
                  （本地网关 `http://127.0.0.1:8080/v1` 就是反例）。 */}
              <label className="field">
                <span>{t("请求地址（可留空）")}</span>
                <input value={form.base_url} maxLength={limits.maxUrl}
                       placeholder={t(addrHint.example)}
                       onChange={(e) => setForm((f) => ({ ...f, base_url: e.target.value }))} /></label>
              <p className="ep-hint">{t(addrHint.note)}</p>

              {/* api 那格不放通用水印 —— 各家与各中转站的格式都不一样，
                  没有一种写法能代表它们。编辑时只提示「留空不改原来那把」。 */}
              <label className="field">
                <span>api{editing ? t("（留空则不改原来那把）") : ""}</span>
                <input type="password" value={form.api}
                       placeholder={editing ? t(apiHint(editing)) : ""}
                       onChange={(e) => setForm((f) => ({ ...f, api: e.target.value }))} /></label>

              {/* 「高级选项」默认收着；勾开才出现容量那一格，
                  且先看到那条红横幅。 */}
              <label className="ep-check">
                <input type="checkbox" checked={form.advanced}
                       onChange={(e) => setForm((f) => ({ ...f, advanced: e.target.checked }))} />
                <span>{t("高级选项")}</span>
              </label>
              {form.advanced && (
                <>
                  <p className="ep-danger">
                    {t("如果你不清楚你的模型上下文容量，请不要更改此项，留空")}
                  </p>
                  <label className="field"><span>{t("模型上下文容量")}</span>
                    <input value={form.window} inputMode="numeric" placeholder={t("留空")}
                           onChange={(e) => setForm((f) => ({ ...f, window: e.target.value }))} /></label>
                  {/* 能力覆盖口 —— 三态，默认「自动判定」。
                      查表查不到时（中转站 / 自建模型），用户只能改 config.yaml，
                      而那管不到本条目。
                      三档的默认是 auto，所以不勾的人行为一个字节不变。 */}
                  <p className="ep-note">
                    {t("图片 / 音频能力默认按模型名查表判定（models.dev 的能力表）。 中转站 / 自建模型查不到、或表判错时，可以在这里自己定；")}
                    <b>{t("不确定就留在「自动判定」")}</b>。
                  </p>
                  <CapModeSelect label={t("图片（视觉）能力")}
                                 value={(form.capabilities || {}).vision}
                                 disabled={busy}
                                 onChange={(v) => setForm((f) => ({
                                   ...f, capabilities: { ...(f.capabilities || {}), vision: v },
                                 }))} />
                  <CapModeSelect label={t("音频能力")}
                                 value={(form.capabilities || {}).audio}
                                 disabled={busy}
                                 onChange={(v) => setForm((f) => ({
                                   ...f, capabilities: { ...(f.capabilities || {}), audio: v },
                                 }))} />
                </>
              )}

              {probe && (
                <p className={"ep-probe" + (probe.pending ? "" : (probe.ok ? " ok" : " bad"))}>
                  {probe.text}
                  {probe.scope_note ? " " + probe.scope_note : ""}
                </p>
              )}

              <div className="ep-actions">
                <button className="skill-btn" disabled={busy || !verdict.ok}
                        title={verdict.error || ""} onClick={save}>
                  <Check size={13} />{editing ? t("保存") : t("建好它")}
                </button>
                {/* 「简易联通测试」：用表单里填的这一份去发（还没保存也能先测） */}
                <button className="skill-btn" disabled={busy || !verdict.ok}
                        title={verdict.error || t("用上面填的地址 + key 真发一次最小请求")}
                        onClick={() => {
                          const body = editing ? buildUpdatePayload(editing.id, form, limits)
                                               : buildCreatePayload(form, limits);
                          if (body) probeOne(body);
                        }}>
                  <Plug size={13} />{t("简易联通测试")}
                </button>
                {editing && (
                  <button className="skill-btn" disabled={busy}
                          onClick={() => { setEditing(null); setForm(blankConfig(openCard)); setProbe(null); }}>
                    {t("取消编辑")}
                  </button>
                )}
                {!editing && !room.ok && <span className="muted">{room.error}</span>}
              </div>

              <p className="ep-note">
                {t("设为默认后，")}<b>{t("新开的对话")}</b>{t("将会启用此配置；正在跑的框架需")}<b>{t("重启一次")}</b>{t("以更换，")}
                {t("所有对话共用一个默认。")}
              </p>
            </div>
          </section>
        </div>
      )}

      {/* 删除的二次确认（用户）：不可逆的动作一次点击不许发生。
          复用既有 `Confirm`（外壳照 `SafetyNotice`，不新造第三种浮窗）；
            `busy` 时两个按钮都禁用 —— 防连点发两次请求。 */}
      {confirmDel ? (
        <Confirm
          title={t("删除这条配置？")}
          body={t("「{name}」会被删除，正在用它的对话下次框架重启后会回落到默认配置。", { name: confirmDel.name })}
          confirmLabel={t("删除")}
          cancelLabel={t("取消")}
          busy={busy}
          onCancel={() => setConfirmDel(null)}
          onConfirm={async () => {
            const row = confirmDel;
            setConfirmDel(null);
            await act("/api/model-configs/delete", { id: row.id }, t("配置已删除。"));
          }} />
      ) : null}
    </section>
  );
}
