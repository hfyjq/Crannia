// 端配置页的判据 —— 纯函数，node 直接断言。
//
//   为什么单独一个文件（照 `group-form.js` 那条）：坏法全在边界上 ——
//   api 该不该发、窗口填没填、按钮该不该灰 —— 而组件里的 `useState` 断不了那些。
//
// 判据的权威在后端（`core/model_configs.py`，那条有 91 条读者）。
//    这里这一份是前端那道，只为两件事服务：
//      ① 不发一个注定 400 的请求；② 按钮该不该灰。
//    所以它不是安全边界，两边的上限也都由后端给（`GET /api/model-configs`
//      回 `max_per_card` / `max_name` / `max_model` / `max_url` / `window_min` /
//      `window_max`）—— 前端不写死任何一个（写死就是第二份真相源）。

// 界面语言：这些判据话术都是给用户看的，而本文件全是函数，在函数里
//   `t()` 是安全的（没有"import 时求值"那个坑）。
import { t } from "./i18n.js";

/** 一张空白的新建表单。`card` 由点开的那张卡给定。 */
export function blankConfig(card = "") {
  return {
    card,
    name: "",
    model: "",
    base_url: "",
    api: "",
    window: "",
    //:能力覆盖三态。默认 auto —— 与现有条目的行为同义（查表 > 默认）。
    //:   另外两档是用户自己的口子：中转站 / 自建模型查不到表时用。
    capabilities: { vision: "auto", audio: "auto" },
    //: 「高级选项」默认收着 —— 红横幅说的是「不清楚就别改」，
    //:那它就该是要先点开才看得到的。
    advanced: false,
  };
}

/** 上下文容量那一格。回 `{ok, value, error}`。
 *
 *  留空是合法的（= 交给"查表 / 演算"那几层去定），不是错。
 *  只在 `advanced` 打开时才判它 —— 收着的时候用户看不到那一格，
 *    拿一个看不见的值去拦提交是最招人烦的一类错。
 */
export function windowVerdict(raw, limits = {}) {
  const min = Number(limits.windowMin) || 1024;
  const max = Number(limits.windowMax) || 10000000;
  const s = String(raw == null ? "" : raw).trim();
  if (!s) return { ok: true, value: null, error: "" };
  if (!/^\d+$/.test(s)) {
    return { ok: false, value: null, error: t("上下文容量要填整数（不确定就留空）") };
  }
  const v = Number(s);
  if (v < min || v > max) {
    return { ok: false, value: null,
             error: t("上下文容量要在 {min} ~ {max} 之间", { min, max }) };
  }
  return { ok: true, value: v, error: "" };
}

/** 能力三态的那一格。回 `{ok, value}`。
 *
 *  只认那三个字符串 —— 与后端 `capabilities_of` 同一张嘴。
 *  这一层只是"不发一个注定 400 的请求"；权威在后端（见文件头）。
 */
export const CAP_MODES = ["auto", "on", "off"];

export function capModeVerdict(raw) {
  const s = String(raw == null ? "auto" : raw).trim().toLowerCase();
  if (!CAP_MODES.includes(s)) {
    return { ok: false, value: "auto",
             error: t("能力只能是 auto / on / off（收到 {raw}）", { raw }) };
  }
  return { ok: true, value: s, error: "" };
}

/** 表单里的能力两格 → 请求体那一格。 */
export function capabilitiesOf(form) {
  const c = (form && form.capabilities) || {};
  return {
    vision: capModeVerdict(c.vision).value,
    audio: capModeVerdict(c.audio).value,
  };
}

/** 整张表单的判据。回 `{ok, error}`，`error` 是给用户看的一句话。 */
export function validateConfig(form, limits = {}) {
  const f = form || {};
  const maxName = Number(limits.maxName) || 40;
  const maxModel = Number(limits.maxModel) || 120;
  const maxUrl = Number(limits.maxUrl) || 300;
  const name = String(f.name || "").trim();
  const model = String(f.model || "").trim();
  const url = String(f.base_url || "").trim();
  if (!model) return { ok: false, error: t("模型名不能空 —— 它就是发到上游的那个串") };
  if (model.length > maxModel) {
    return { ok: false, error: t("模型名太长（{model.length} > 上限 {maxModel} 字）",
                                { "model.length": model.length, maxModel }) };
  }
  if (name.length > maxName) {
    return { ok: false, error: t("配置名称太长（{name.length} > 上限 {maxName} 字）",
                                { "name.length": name.length, maxName }) };
  }
  if (url.length > maxUrl) {
    return { ok: false, error: t("请求地址太长（{url.length} > 上限 {maxUrl} 字）",
                                { "url.length": url.length, maxUrl }) };
  }
  if (url && !/^https?:\/\//i.test(url)) {
    return { ok: false, error: t("请求地址要以 http:// 或 https:// 开头") };
  }
  if (f.advanced) {
    const w = windowVerdict(f.window, limits);
    if (!w.ok) return { ok: false, error: w.error };
    //:三态只在展开时判（收着时用户看不到那两格）。
    //:判的是原件、不是 `capabilitiesOf(f)` —— 后者把坏值夹成 `auto`
    //:   （它服务于"发请求"），拿它来判等于永远通过。
    const c = f.capabilities || {};
    for (const key of ["vision", "audio"]) {
      const v = capModeVerdict(c[key]);
      if (!v.ok) return { ok: false, error: v.error };
    }
  }
  return { ok: true, error: "" };
}

function _windowOf(form, limits) {
  if (!form || !form.advanced) return null;      // 收着 = 交给后端那几层
  return windowVerdict(form.window, limits).value;
}

/** 建配置的请求体。不合法回 `null`（同 `group-form.buildPayload` 那条：
 *  不发一个注定 400 的请求）。 */
export function buildCreatePayload(form, limits = {}) {
  if (!validateConfig(form, limits).ok) return null;
  return {
    card: form.card,
    name: String(form.name || "").trim(),
    model: String(form.model || "").trim(),
    base_url: String(form.base_url || "").trim(),
    api: String(form.api || "").trim(),
    window: _windowOf(form, limits),
    capabilities: capabilitiesOf(form),
  };
}

/** 改配置的请求体。
 *
 * `api` 只有用户真填了才发 —— 缺席 = 后端一个字不动那格凭据。
 *   这条很容易写反：写成"总会发 `api:""`"就等于每次改名都把 key 清掉，
 *   而界面上一点异样都没有（"静默改掉用户的东西"）。
 */
export function buildUpdatePayload(id, form, limits = {}) {
  if (!validateConfig(form, limits).ok) return null;
  const body = {
    id,
    name: String(form.name || "").trim(),
    model: String(form.model || "").trim(),
    base_url: String(form.base_url || "").trim(),
    window: _windowOf(form, limits),
    capabilities: capabilitiesOf(form),
  };
  const api = String(form.api || "").trim();
  if (api) body.api = api;
  return body;
}

/** 一条已存的配置反解成表单（与新建共用同一张表单）。
 *
 * `api` 那格故意留空（不是填回明文 —— 界面根本拿不到它）。
 *   空 = "不改"，用户想换 key 就填一把新的。
 */
export function entryToForm(entry) {
  const e = entry || {};
  //:编辑时回填用户选的那一档（`capability_modes`），
  //:   而不是从行为值反推 —— 行为 `true` 可能是表给的，也可能是用户勾的。
  const modes = e.capability_modes || {};
  const capabilities = {
    vision: capModeVerdict(modes.vision).value,
    audio: capModeVerdict(modes.audio).value,
  };
  const anyOverride = capabilities.vision !== "auto" || capabilities.audio !== "auto";
  return {
    card: e.card || "",
    name: e.name || "",
    model: e.model || "",
    base_url: e.base_url || "",
    api: "",
    window: e.window == null ? "" : String(e.window),
    capabilities,
    //:有覆盖时自动展开高级区（否则用户改过的档在界面上看不见，
    //:下一次保存会静默把它带回去 —— 「标签在说谎」同族）。
    advanced: e.window != null || anyOverride,
  };
}

/** 凭据那一格怎么显示。只给尾 4 位（认得出是哪把，但不泄密）。 */
export function apiHint(row) {
  const r = row || {};
  if (!r.has_api) return t("未填");
  return r.api_tail
    ? t("已保存 · 尾 {tail}", { tail: r.api_tail })
    : t("已保存");
}

/** 一张卡片的副标题：几条配置 / 上限。 */
export function cardSubtitle(card, maxPerCard) {
  const n = ((card && card.configs) || []).length;
  const cap = Number(maxPerCard) || 10;
  return `${n} / ${cap}`;
}

/** 「这张卡还能不能再建一条」—— 满了就灰，且说得出为什么。 */
export function canAdd(card, maxPerCard) {
  const n = ((card && card.configs) || []).length;
  const cap = Number(maxPerCard) || 10;
  return { ok: n < cap,
           error: n < cap ? "" : t("这张卡最多 {cap} 条配置，先删一条。", { cap }) };
}

/** 联通测试的结果怎么显示成一句话（`data` 是端点回的载荷）。 */
export function testVerdict(data) {
  const d = data || {};
  //: 后端给的那句是唯一口径（`message`）；只有它缺席时才在这里拼一句兜底。
  if (d.message) return { ok: !!d.ok, text: d.message };
  if (d.ok) {
    return { ok: true, text: t("已联通（HTTP {status}）", { status: d.status }) };
  }
  if (d.status === 0) {
    return { ok: false, text: d.body || t("连接失败") };
  }
  return { ok: false,
           text: t("上游回了 HTTP {status}", { status: d.status })
                 + (d.body ? "：" + d.body : "") };
}
