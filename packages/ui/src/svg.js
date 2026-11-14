// SVG 呈现的判据 + 消毒配置（纯逻辑）。
//
// 拆出来是为了能跑 node 测试：
//   · `svg-preview.test.mjs` —— 纯函数（根元素判定 / data URL / 上限）；
//   · `svg-sanitize.test.mjs` —— 真的过一遍 DOMPurify（用 jsdom 造 window）。
// `import DOMPurify from "dompurify"` 在 node 里不会建 DOM（它返回工厂），
//    所以这个模块在 node 里 import 是安全的；`sanitizeSvg` 第二个参数可以换成
//    一个绑定了 jsdom window 的实例（见那个测试）。
// 判据同 `markdown.js` / `group-form.js`：能纯函数化的就纯函数化，
//    界面那一层只负责接 React。

import DOMPurify from "dompurify";

/** 输入上限。超过就只显示源码。 */
export const MAX_SVG_CHARS = 200 * 1024;

/** 这串文本看起来是不是一张 SVG。
 *
 *  判据与后端 `core/artifacts._sniff_av` 逐字同一条：根元素是 `<svg>`
 *  ——`<?xml …?>` 允许在前面，但正文里出现 `<svg` 不算（那种东西
 *  更像一段说明文字，不是图）。 */
export function isSvgSource(text) {
  const s = String(text == null ? "" : text).replace(/^﻿/, "").trimStart();
  if (!s) return false;
  const low = s.slice(0, 1024).toLowerCase();
  if (low.startsWith("<svg")) return true;
  return low.startsWith("<?xml") && low.includes("<svg");
}

/** 消毒之后的 SVG 文本 → `data:` URL。
 *
 *  用 `<img>` 装它（不是 `innerHTML`）：图片文档里的脚本物理上不执行，
 *  而 SMIL（`<animate>` 一族）照常播放。代价是 CSS/JS 动画不参与（有意）。
 *  `encodeURIComponent` 处理非 ASCII —— 模型写的 `title` 里常有中文。 */
export function svgDataUrl(clean) {
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(String(clean || ""));
}

/** 超长？超了就只给源码 + 一句说明（不白屏）。 */
export function tooLarge(text) {
  return String(text == null ? "" : text).length > MAX_SVG_CHARS;
}

/** DOMPurify 的配置（一处定义，`SvgPreview` 与 Mermaid 那条都走它）。
 *
 *  `USE_PROFILES.svg` 把允许集收窄到 SVG 子集；`ADD_TAGS` 显式放行：
 *    - SMIL 三件 + `set`/`mpath`（动图要能播）；
 *    - `style` —— 模型画的图与 Mermaid 的产物
 *      都把样式写在 `<style>` 里（`.wheel-rim{fill:none;stroke:#333}` 之类）。
 *      剥掉它的后果是轮子变成实心黑圆、车架整条消失
 *      （没有 stroke，线宽为零）。必须留。
 *  `FORBID_TAGS` 只压 `script` 与 `foreignObject`（后者能把任意 HTML 塞进 SVG）。
 *  事件属性（`on*`）DOMPurify 默认就清掉，不用手写通配（它也不支持通配）。
 *
 *  ## 为什么留着 `<style>` 是安全的
 *
 *  这整份 SVG 最终装进 `<img src="data:image/svg+xml,…">` —— 浏览器按
 *  secure static mode 渲染它：脚本不执行、外部资源不加载（CSS 里的
 *  `@import` / 外链 `url()` 也一样不取）。`<style>` 能做的只有"在图片内部
 *  排版/上色/做动画"，做不了发请求、也拿不到页面上下文。
 *  这条判据依赖"它是 `<img>`" —— 哪天改成 `innerHTML` / 内联 iframe，
 *  这条就不成立了（`svg-preview.test.mjs` 钉着"不许 DOM 注入"）。
 */
export const SVG_SANITIZE_CONFIG = {
  USE_PROFILES: { svg: true, svgFilters: true },
  ADD_TAGS: ["animate", "animateTransform", "animateMotion", "set", "mpath",
             "style"],
  FORBID_TAGS: ["script", "foreignObject"],
};

/** 消毒一段 SVG 文本；不可用，空串。
 *
 *  *purify* 默认是模块里的 DOMPurify（浏览器里它绑好了 window）；
 *  node 测试传一个 `createDOMPurify(jsdomWindow)` 进来的实例。
 */
export function sanitizeSvg(code, purify) {
  const p = purify || DOMPurify;
  const clean = p.sanitize(String(code == null ? "" : code),
                           SVG_SANITIZE_CONFIG);
  return isSvgSource(clean) ? clean : "";
}

/** 给下载用的文件名（`diagram.svg` / `chart-2.svg` …）—— 纯装饰，不进判据。 */
export function downloadName(text, prefix = "diagram") {
  const s = String(text || "");
  const t = /<title[^>]*>([^<]{1,40})<\/title>/i.exec(s);
  const base = (t ? t[1] : prefix).replace(/[\\/:*?"<>|\s]+/g, "-").trim();
  return (base || prefix) + ".svg";
}
