// 界面语言的前端运行时—— 与后端 `core/i18n.py` 同一条判据。
//
//   · 键是中文原文；包由 `GET /api/i18n` 下发（`{中文: English}`）。
//   · 查不到，回落原文（永不显示裸键，与后端逐字同一条）。
//   · `t(text, vars)`：有占位符时第二个参数是名字→值的映射
//     （`t("共 {n} 条", { n: 3 })`）—— 按名字替换，所以译文可以调语序。
//   · 不 import 任何 React 东西：这个模块要能被 node 自检直接跑。

let LANG = "zh";
let PACK = Object.freeze({});

/** 装上语言包（`lang` 是当前界面语言；`pack` 是 `{中文原文:译文}`）。 */
export function setPack(lang, pack) {
  LANG = String(lang || "zh");
  PACK = pack && typeof pack === "object" ? Object.freeze({ ...pack }) : Object.freeze({});
}

export function lang() {
  return LANG;
}

/** 把一句界面文案换成当前语言；查不到，原文。
 *
 *  首尾空白先摘掉再查、查完原样放回去 —— 与后端 `core/i18n.tr_fmt`
 *  逐字同一条判据（那边 docstring 写着"表里的键是 strip 过的"）。
 *  不摘的话，拿空格当分隔符的那种键永远查不到：
 *  `t(" · 下一次 {at}")` 去表里找带前导空格的键，而表里是 `· 下一次 {at}`
 *，英文界面里它们一直是中文（`· 当前` / `· 已暂停` / `· 没连上` /
 *  `　右键可删除` 一族，一条条数出来的）。
 */
export function t(text, vars) {
  if (!text) return text;
  const core = text.trim();
  if (!core) return text;
  const head = text.slice(0, text.indexOf(core));
  const tail = text.slice(head.length + core.length);
  const tpl = Object.prototype.hasOwnProperty.call(PACK, core) ? PACK[core] : core;
  //:表里不是字符串，回落原文（不是把它拼进去）。
  //:拼进去会走 `head + tpl + tail`，把一个函数拼成源码文本（`String(fn)`
  //:   就是那个函数的源码）—— 界面上会出现一屏看不懂的代码，而不是一句中文。
  //:那屏压缩 JS 的真根因在别处（`App.jsx` 的 `delta`
  //:把 `t` 这个函数拼进了草稿）；这条是纵深：语言包里出现
  //:   坏数据时退化成原文，而不是变成代码。
  if (typeof tpl !== "string") return text;
  if (!vars) return head + tpl + tail;
  return head + tpl.replace(/\{[^{}]*\}/g, (m) => {
    const name = m.slice(1, -1);
    return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : m;
  }) + tail;
}

/**
 * 拉一次界面语言包（页面加载时调用一次）。
 *
 * 任何失败都安静回落到原文 —— 语言包读不到不该让界面白屏
 * （与后端 `t()` 「在渲染路径上不许抛」同一条）。
 */
export async function loadI18n(client) {
  try {
    // 必须走 `client.call`（它带 `X-Crannia-Token`）。裸 `fetch` 会被
    //    `/api/i18n` 的令牌守门 403 掉，而这里又"安静回落"
    //    界面全中文、控制台一声不响（选了 English 没反应）。
    const data = client && typeof client.call === "function"
      ? await client.call("/api/i18n")
      : await (await fetch("/api/i18n", { cache: "no-store" })).json();
    if (data && data.ok) {
      setPack(data.lang, data.pack || {});
      return data.lang;
    }
  } catch (err) {
    // 安静回落：默认 zh 就是原文（但别把"没拿到"当成"没有" —— 见上）
  }
  return LANG;
}
