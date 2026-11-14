// 「上下文用了多少」那一族 —— 纯格式化 + 纯映射，不认识 React。
//
// 为什么单独一个文件：本包没有 vitest/jest，而"写一个格式化器却不配测试"
//    不可接受，写成纯函数，`node` 直接跑断言
//    （`packages/ui/tests/context.test.mjs` + `tests/test_desktop_context.py`，
//     形状照 `format.js` 那一对）。
//
// 三条判据，逐条落在下面这些函数里：
//    ① 读不到一律是「—」，不是 `0` —— "没量到"与"量到了没变"
//       是两件事，而 `|| 0` 会把它们塌成同一句话（那正是这条判据防的）。
//    ② 分母要说清：窗口那个值是声明值，来源必须跟着值走，悬停里
//       一定带 `来源:` 那一段（读的人要能判断它是不是一个保守兜底）。
//    ③ 「与上次比」那一列才是信号 —— 单看绝对数没有感觉；而第一次打开时
//       每一格都读不到（没有"上次"），那时整列都是「—」，这是对的。
//
// 它与 WebUI 的 `chat.html` 是同一套语义（那份是原型、这份是新前端）：
//    顶栏那条读数与面板那三列逐字对齐——改一边必须问另一边，
//    否则同一个数在两套前端上会讲两句不同的话。

/** 精确千分位。读不到 → `"—"`（判据 ①）。
 *
 *  它和 `format.js::fmtCount` 是两个量，别换着用：`fmtCount` 给的是
 *  `1.1k` 这种缩写（给"读着顺"的地方），而这里是精确值 ——
 *  面板上那三列要能对得上账（尤其"与上次比"那一列：`+1.1k` 看不出涨了多少）。
 */
import { t } from "./i18n.js";
export function fmtInt(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** token 数的缩写形式（`12.3K tok`）。只用在一句话里（本轮压缩的进出），
 *  它不需要对账、只需要读得下去。 */
export function fmtTok(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  if (n < 1000) return n + " tok";
  if (n < 1000000) return (n / 1000).toFixed(1) + "K tok";
  return (n / 1000000).toFixed(1) + "M tok";
}

//:没读数时那一句。它是占位不是空串：面板入口就挂在这行字上，
//:而"没读数"是一个常见状态（整页重载、`/clear` 之后）—— 空串 = 没有可点区域
//: = 面板在那段时间里根本进不去。
export const READOUT_EMPTY = "上下文 —";

export const READOUT_EMPTY_TITLE = "本会话暂无数据，下一轮后显示。";

/** 空态那句悬停的第二半（拼在取用处，不在模块级拼）。 */
const EMPTY_TITLE_MORE = "点这里可以直接看会话构成面板。";

/** 顶栏那一行读数：载荷 → `{text, cls, title}`。
 *
 *  * 没读数（`pct` 不是数字），空态那一句（仍然可点，见上面那条）。
 *  * 有读数，`上下文 12% / 阈值 85%`；阈值是百分比不是 token ——
 *    token 绝对值只在悬停里（面板那一侧才给精确值）。
 *  * 本轮真的压过，整句换成 `本轮已压缩 12.3K tok→3.1K tok，现 22%`。
 *    只在"本轮压过"时出现：后端保证不压就不带这一栏，否则粘住的旧值
 *    会伪装成新事实（`compressed` 是"这一次"的读数）。
 *  * `armed`（已经到阈值附近），追加 ` · 接近触发`。
 */
export function readoutText(d) {
  // 这里每个字都是拼出来的，一律走带占位符的模板
  //   （`t("上下文 {pct}%", { pct })）—— 拿碎片拼英文会语序不对。
  //   App.jsx 那边还会 `t(readout.text)` 一次 —— 译文再进一次
  //   `t()` 会查不到就原样返回，无害。
  if (!d || typeof d.pct !== "number") {
    return { text: t(READOUT_EMPTY), cls: "",
             title: t(READOUT_EMPTY_TITLE) + t(EMPTY_TITLE_MORE) };
  }
  let text = typeof d.threshold_pct === "number"
    ? t("上下文 {pct}% / 阈值 {thr}%", { pct: d.pct, thr: d.threshold_pct })
    : t("上下文 {pct}%", { pct: d.pct });
  let cls = "";
  const c = d.compressed;
  if (c && typeof c.pre_tokens === "number" && typeof c.post_tokens === "number") {
    text = t("本轮已压缩 {pre}→{post}，现 {pct}%",
             { pre: fmtTok(c.pre_tokens), post: fmtTok(c.post_tokens), pct: d.pct });
    cls = "compressed";
  } else if (d.armed) {
    text += t(" · 接近触发");
    cls = "armed";
  }
  // 判据 ②：来源跟着值走。窗口是声明值，不给来源，读的人无从判断
  //   它是不是一个保守兜底（兜底值 250,000 是有意偏保守的）。
  const title = t("窗口 {win} tok（来源: {src}） · 阈值 {thr} tok · 本轮消息 {msgs} 条 · 校准因子 {f}",
    { win: d.window, src: d.window_source || "?", thr: d.threshold_tokens,
      msgs: d.messages, f: d.factor });
  return { text, cls, title };
}

/** 「与上次比」那一格：`{text, cls}`。读不到 → `"—"` 且不加类（判据 ①③）。 */
export function deltaText(delta) {
  if (typeof delta !== "number" || !Number.isFinite(delta)) return { text: "—", cls: "" };
  if (delta > 0) return { text: "+" + fmtInt(delta), cls: "up" };
  if (delta < 0) return { text: fmtInt(delta), cls: "down" };
  return { text: "0", cls: "" };
}

/** 面板里那一行的名字 —— 子集行带 `↳ ` 前缀。
 *
 *  `subset_of` 的存在就是"不参与加总"这个事实本身（后端没有另设一个
 *  布尔字段）：只有「技能索引」是「对话历史」的子集，合计那一行不含它。
 */
export function cellLabel(cell) {
  const name = (cell && cell.label) || "";
  return (cell && cell.subset_of ? "↳ " : "") + t(name);
}

/** 面板底下那一行合计。最后那半句不许省 ——
 *  不说清"加总不含哪一格"，读的人会自己去做一道他没被告知的减法。 */
export function noteText(d) {
  if (!d) return "";
  return t("合计 {sum} tok · 窗口 {win}（{src}）· 占 {pct}% · 阈值 {thr} tok · 校准因子 {f}",
           { sum: fmtInt(d.estimated_tokens), win: fmtInt(d.window),
             src: d.window_source || "?", pct: d.pct,
             thr: fmtInt(d.threshold_tokens), f: d.factor })
    + t("（上表已乘）")
    + t("　（合计不含「技能索引」，它已计入对话历史）");
}

/** 面板每一行要渲染的三格 —— 一次算准，省得 JSX 里各拼一遍。
 *  返回 `[{key, label, val, delta, deltaCls, source}]`。
 */
export function cellRows(d) {
  return ((d && d.cells) || []).map((c) => {
    const dt = deltaText(c.delta);
    return {
      key: c.key,
      label: cellLabel(c),
      val: fmtInt(c.tokens) + " tok",
      delta: dt.text,
      deltaCls: dt.cls,
      source: c.source || "",
    };
  });
}

/** 会话构成面板：这一次 `mousedown` 该不该把它关掉
 *  （它不能只能靠 ✕ 关，点别处也要能关）。
 *
 *  判据 = 点在面板外、且不在那条读数上，关。
 *  读数那一颗按钮必须排除 —— 它自己的 `onClick` 是开合
 *    （`App.jsx::toggleCtx`）；两边都响应的话，点它就变成"先关、再开"
 *，再也关不上（同 `Rail.jsx::Drawer` 里 `.rail` 那一条，
 *    `test_main_ui` 钉着那个形状）。
 *  面板与读数不在同一个 host 里（面板挂在 `.main` 上、读数在 `header` 里）
 *，这里是两个选择器，而不是 `Composer` 那种一个 `.popover-host`。
 *  拿不到 `closest`（合成事件 / 旧环境），当真（关）：宁可多点一下
 *    再打开，也别让它关不掉。
 */
export function ctxPanelCloses(target) {
  if (!target || typeof target.closest !== "function") return true;
  // 批 5：顶栏那两枚任务 chip 也开着面板 —— 不排除它们的话，
  // mousedown 先关、click 再开，看着"点了没反应"（与 `.ctx-readout` 同一条）。
  return !target.closest(".ctx-panel") && !target.closest(".ctx-readout")
      && !target.closest(".task-chip");
}
