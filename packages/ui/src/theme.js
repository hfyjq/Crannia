// GUI 的配色 —— 六个轴 + GUI 自己的存储。
//
// 存哪：GUI 自己的 localStorage。
//   · 键照隔壁 `lv.sideW`（侧栏宽度）的命名：`lv.colors`。
//   · 为什么不跟 WebUI 共用：GUI 跑在 `crannia-app://`，WebUI 跑在
//     `http://127.0.0.1:5000` —— 两个 origin 的 localStorage 天然不通；
//     真要共用只能走后端（那要新端点 + 让 WebUI 也改读它，是另一件事）。
//   代价如实说：两个前端各调各的。这是有意的取舍，不是漏掉的一半。
//
// 六个轴：
//   左边栏底色 / 左边栏文字 / 大背景 / 框底色 / 框内文字 / 选择高亮。
//
// 当前边界（写在这里，免得以后误以为"换色没生效"）：
//   · 每个轴只驱动它指向的那一个令牌（`styles.css` 里 `--th-*` 已经接好了）；
//   · 整张表的 342 个令牌不跟着推导 —— 比如侧栏的 hover 色仍是原设计那个绿。
//   · 要整表跟着变，下一步得把 `app-bg` / `app-fg` 再拆成"大背景 / 框"两族，
//     再由六个轴算出来（那需要量过，别盲做）。
//
// 纯模块：不碰 DOM、不读 `window` —— 存储和文档对象都由调用方传进来
//   （`applyTheme(colors, doc)`），所以 `node` 里可以逐条断言（见
//   `packages/ui/tests/theme.test.mjs`）。

/** GUI 自己的存储键（同族：`lv.sideW`）。 */
export const THEME_KEY = "lv.colors";

/** 六个主题轴。`cssVar` 是 `styles.css` 里已经定义好的那一个。
 *
 *  `def` 必须与 `styles.css` 的 `--th-*` 逐字相等 ——
 *  它是"恢复默认"要写回去的值，也是 `themeCss` 补空缺时用的值。
 *  两处漂了就会「点了恢复默认、观感没回来」，而没有任何东西会红。
 *，`tests/test_desktop_theme.py` 会拿这一张表去对 `styles.css`。
 */
export const AXES = [
  { key: "sideBg", cssVar: "--th-side-bg", label: "左边栏底色", def: "#e7eee8" },
  { key: "sideFg", cssVar: "--th-side-fg", label: "左边栏文字", def: "#394b40" },
  { key: "appBg", cssVar: "--th-app-bg", label: "大背景", def: "#f7f9f6" },
  { key: "boxBg", cssVar: "--th-box-bg", label: "框底色", def: "#ffffff" },
  { key: "boxFg", cssVar: "--th-box-fg", label: "框内文字", def: "#293a30" },
  { key: "select", cssVar: "--th-select", label: "选择高亮", def: "#c8dfcb" },
];

/** 深色下六个轴的默认值。
 *
 *  必须与 `styles.css` 里生成块的 `--th-*` 逐字相等 ——
 *  `scripts/derive_dark_palette.py::DARK_AXES` 是另一处来源，两处一起改。
 */
export const DARK_DEFAULTS = {
  sideBg: "#17181a",
  sideFg: "#cfd2d4",
  appBg: "#0f1011",
  boxBg: "#191a1c",
  boxFg: "#e6e7e8",
  select: "#2e3238",
};

/** 主题模式（`lv.theme`）—— 默认跟随系统。 */
export const MODE_KEY = "lv.theme";

export const THEME_MODES = [
  { key: "light", label: "浅色" },
  { key: "dark", label: "深色" },
  { key: "system", label: "跟随系统" },
];

/** 深色配色的存储键（浅色那份仍是 `lv.colors`，老数据不用搬）。 */
export const DARK_THEME_KEY = "lv.colors.dark";

/** 某个模式下六个轴的默认值。 */
export function defaultsFor(mode) {
  const defs = mode === "dark" ? DARK_DEFAULTS : null;
  const out = {};
  for (const a of AXES) out[a.key] = defs ? defs[a.key] : a.def;
  return out;
}

/** 六个轴的默认值（浅色那份；保留旧名字给老调用方）。 */
export function allDefaults() {
  return defaultsFor("light");
}

/** 把 `lv.theme` 读出来（认不出，`system`，不抛）。 */
export function loadMode(storage) {
  try {
    const v = storage.getItem(MODE_KEY);
    return THEME_MODES.some((m) => m.key === v) ? v : "system";
  } catch {
    return "system";
  }
}

export function saveMode(mode, storage) {
  try {
    storage.setItem(MODE_KEY, THEME_MODES.some((m) => m.key === mode) ? mode : "system");
    return true;
  } catch {
    return false;
  }
}

/** `system`，看系统；其余原样。`mql` 是 `matchMedia('(prefers-color-scheme: dark)')`。 */
export function resolveMode(pref, mql) {
  if (pref === "dark" || pref === "light") return pref;
  try {
    return mql && mql.matches ? "dark" : "light";
  } catch {
    return "light";
  }
}

/** 把解析后的模式打到文档上（`data-theme` + `color-scheme`）。
 *
 *  `color-scheme` 管的是原生控件（滚动条 / 输入框 / 下拉）——
 *  不设它，深色下那几处会白得刺眼。
 */
export function applyMode(resolved, doc) {
  const d = doc || (typeof document === "undefined" ? null : document);
  if (!d || !d.documentElement) return;
  const dark = resolved === "dark";
  d.documentElement.dataset.theme = dark ? "dark" : "light";
  try { d.documentElement.style.colorScheme = dark ? "dark" : "light"; } catch { /* 老浏览器 */ }
}

/** 是不是一个安全的 `#rrggbb`。
 *
 *  只收六位十六进制：三个来源都可能是坏值（用户手改 localStorage、
 *  老版本遗留、以后导入配置），而 `input type=color` 只吐这一种。
 *  坏值不抛错、直接丢掉 —— 丢一格 = 那个轴用默认值，
 *  比整份配色不生效好（同 `读不到，默认` 那条）。
 */
export function isHex(v) {
  return typeof v === "string" && /^#[0-9a-fA-F]{6}$/.test(v);
}

/** 只留下认识的键 + 合法的值（其余全丢）。 */
export function normalize(raw) {
  const out = {};
  if (!raw || typeof raw !== "object") return out;
  for (const a of AXES) {
    if (isHex(raw[a.key])) out[a.key] = raw[a.key].toLowerCase();
  }
  return out;
}

/** 这份配色有没有改过（和这个模式的默认值逐格比）。 */
export function changedCount(colors, mode = "light") {
  const c = normalize(colors);
  const defs = defaultsFor(mode);
  return AXES.filter((a) => c[a.key] && c[a.key] !== defs[a.key]).length;
}

/** 从存储读（读不到 / 坏 JSON，空对象，用默认值，不抛）。
 *
 *  两套各存各的：浅色在 `lv.colors`（老数据原样有效），深色在 `lv.colors.dark`。
 *，两种主题下各调各的，互不打架；也不需要在老数据上做迁移。
 */
export function loadTheme(storage, mode = "light") {
  try {
    const key = mode === "dark" ? DARK_THEME_KEY : THEME_KEY;
    return normalize(JSON.parse(storage.getItem(key) || "{}"));
  } catch {
    return {};
  }
}

/** 写回存储（只存认识的键）。 */
export function saveTheme(colors, storage, mode = "light") {
  try {
    const key = mode === "dark" ? DARK_THEME_KEY : THEME_KEY;
    storage.setItem(key, JSON.stringify(normalize(colors)));
    return true;
  } catch {
    return false;                       // 隐私模式 / 配额满：不抛，界面照旧生效
  }
}

/** 清掉存储（＝恢复默认）。只清当前主题那一份。 */
export function clearTheme(storage, mode = "light") {
  try {
    storage.removeItem(mode === "dark" ? DARK_THEME_KEY : THEME_KEY);
  } catch { /* 同上 */ }
}

/** 生成要注入的 CSS。空字符串 = 没有自定义、什么都别注入。
 *
 *  这是"默认观感一个像素都不变"的保证：没存过，不注入任何东西
 *，用的就是 `styles.css` 里的原值。
 *
 *  `.sidebar{color:…}` 只在左边栏文字被改过时才加：
 *  侧栏里大量文字是继承父级颜色的（`.brand b` / `.space` / `.profile`），
 *  而继承没法用变量直接指 —— 只能给容器补一条 `color`。
 *  但默认值下不许加：那会把继承色从 `#293a30` 改成 `#394b40`，
 *  等于"没配色也在改配色"。
 */
export function themeCss(colors, mode = "light") {
  const c = normalize(colors);
  if (!Object.keys(c).length) return "";
  const defs = defaultsFor(mode);
  const lines = AXES.map((a) => `  ${a.cssVar}: ${c[a.key] || defs[a.key]};`);
  //:选择器必须带 `[data-theme]`：`styles.css` 的深色块是
  //:   `:root[data-theme="dark"]`（比 `:root` 多一个属性），用裸 `:root` 写自定义配色
  //:   会被它按特异性压掉 —— 症状是"深色下调六个轴没反应"，而浅色一切正常。
  const sel = `:root[data-theme="${mode === "dark" ? "dark" : "light"}]`;
  let css = sel + "{\n" + lines.join("\n") + "\n}\n";
  //:判据是"和这个模式的默认值不同" —— 浅色的默认值去比深色那份，
  //:   会把"没改过"误判成"改过"（侧栏继承色跟着被顶掉）。
  if (c.sideFg && c.sideFg !== defs.sideFg) {
    css += ".sidebar{color:var(--th-side-fg)}\n";
  }
  return css;
}

/** 把配色注入文档（`<style id="lv-theme">`）。
 *
 *  只维护这一个元素：重复调用是替换内容，不是再插一个。
 *  `colors` 为空（恢复默认），把那个元素摘掉，回到 `styles.css` 的原值。
 */
export function applyTheme(colors, doc, mode = "light") {
  const d = doc || (typeof document === "undefined" ? null : document);
  if (!d) return;
  const css = themeCss(colors, mode);
  let el = d.getElementById("lv-theme");
  if (!css) {
    if (el) el.remove();
    return;
  }
  if (!el) {
    el = d.createElement("style");
    el.id = "lv-theme";
    d.head.appendChild(el);
  }
  el.textContent = css;
}
