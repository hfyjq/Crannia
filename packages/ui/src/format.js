// 两个纯格式化函数 —— 状态横幅上那两个数（进出都是普通值，不认识 React）。
//
// 为什么单独一个文件：本包没有测试设施（没有 vitest/jest），而
//    "写一个格式化器却不配测试"不可接受。写成纯函数
//    能用 `node` 直接跑断言，不用 JSX、不用打包器、不装任何东西
//    （见 `packages/ui/tests/format.test.mjs`，Python 那侧是 `tests/test_desktop_format.py`）。
//    同族先例：`markdown.js` + `Markdown.jsx` 那一对。
//
// 判据：绿色横幅上的用时与 token 都要量级一变，单位就换
//   （`1m23s` / `1.1k` / `1.1m`）。`169.0s` 要求读者自己心算，
//   而长活恰恰是它最常见的形态（一跑几十步、几分钟）。

/** 秒 → 越读越短。
 *
 *  10 秒以内两位小数（第一眼要是 `0.75s` 而不是 `0.8s`）·
 *  一分钟以内一位小数 · 一分钟以上 `1m23s` · 一小时以上 `1h5m`。
 *  小时档不再给秒：到那个量级秒没有信息量，而宽度是有限的。
 */
import { t } from "./i18n.js";

export function fmtDuration(s) {
  if (!(s >= 0)) return "";            // NaN / 负数，空串，不是 "NaNs"
  if (s < 10) return s.toFixed(2) + "s";
  if (s < 60) return s.toFixed(1) + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m" + Math.floor(s % 60) + "s";
  return Math.floor(m / 60) + "h" + (m % 60) + "m";
}

/** 一步的用时：< 1 秒给毫秒，≥ 1 秒换成秒（`8574ms`，`8.6s`）。
 *
 *  用户「步骤流里大于 1s 的时间就用秒为单位，不要再用 ms 了」。
 *  判据是这一档数值的读法变了，不是"统一换单位"：
 *    50ms 写成 `0.1s` 会把"快"这件事抹掉；而 `8574ms` 要读者心算 ——
 *    两边都难读，所以按量级换单位（同 `fmtDuration` 那条判据）。
 */
export function fmtStepMs(ms) {
  if (ms == null || !(ms >= 0) || !isFinite(ms)) return "";
  if (ms < 1000) return Math.round(ms) + "ms";
  return fmtDuration(ms / 1000);
}

/** 计数 → `1.1k` / `1.1m`。
 *
 *  一千以下原样给：`980` 比 `0.98k` 好读，而且它本来就是精确值。
 *  结尾的 `.0` 去掉（`1k` 而不是 `1.0k`）—— 小数点后那位是信息不是格式。
 *  换单位的判据用"四舍五入之后"，不是用原始值：`999999` 若按
 *    `n < 1e6` 走 k 档，会四舍五入成 `1000k` —— 一个没人这么读的数。
 *，先算 `n/1000`，它到 999.5 就往上一档搬（`999999`，`1m`）。
 */
export function fmtCount(n) {
  if (!(n >= 0)) return "";            // 同上
  if (n < 1000) return String(Math.round(n));
  const one = (v) => v.toFixed(1).replace(/\.0$/, "");
  let v = n / 1000, u = "k";
  if (v >= 999.5) { v = n / 1e6; u = "m"; }
  if (v >= 999.5) { v = n / 1e9; u = "g"; }
  return one(v) + u;
}

/** 模型连接重试那条红横幅上的一句话。
 *
 *  措辞示例：「网络连接异常--请检查宽带互联网连接」。
 *  这里只按类别给话，不显示原始异常（`Connection error.` 那种是
 *  给诊断用的，放在最终报错那一段的最后一行；横幅上它既不完整也不可操作）。
 *  认不出的类别不装懂 —— 给一句中性的，并保留重试次数。
 */
export function retryLabel(kind, text) {
  //: 后端给的那一句优先（`core/llm_errors.HEADLINES` 是唯一口径）——
  //: 它有余额 / 参数 / 5xx / 内容审核这些类别，这里不重复一张表。
  const said = String(text || "").trim();
  if (said) return said;
  const k = String(kind || "").toLowerCase();
  if (k === "timeout") return t("网络连接超时——请检查网络或代理");
  if (k === "connection") return t("网络连接异常--请检查宽带互联网连接");
  return t("模型服务连接异常——正在尝试重新连接");
}

/** 横幅右侧那格：「正在重试 1/10」。坏值不猜（`retry`/`max` 都得是正整数）。 */
export function retryCounter(retry, max) {
  const r = Number(retry), m = Number(max);
  if (!Number.isInteger(r) || !Number.isInteger(m) || r < 1 || m < 1) return "";
  return t("正在重试 {n}/{max}", { n: Math.min(r, m), max: m });
}
