// 兼容性警告弹窗的纯判据 —— 「一条」还是「一批」。
//
// 有多个插件不支持时：正文说「部分插件」，冲突栏显示多个名字和位置。
//
// 判据只有一条，而它必须成对：
//   · 恰好一条，正文仍逐字说「此插件」（不许为了省事一律说"部分"
//     —— 那是标签在说谎：只有一个，却说得像有好几个）
//   · 两条以上，说「部分插件」，冲突栏列多条
//   只写一边的话，「永远说部分」或者「永远说此」总有一种能全绿。
//
// 为什么单开一个文件（而不是塞在 `PluginNotice.jsx` 里）：本包没有 vitest/jest，
//   而"写一条判据却不配测试"不可接受，纯函数，`node` 直接跑断言
//   （`packages/ui/tests/plugin-notice-view.test.mjs` + `tests/test_plugin_notice_view.py`，
//   形状照 `skills-view.js` / `context.js` / `mode.js` 那几对）。
//   `.jsx` 那个文件 `node` 读不了（JSX 语法），所以判据必须在没有 JSX 的文件里。

/** 正文里指代那个/那些插件的那几个字。 */
import { t } from "./i18n.js";

export const SUBJECT_SINGULAR = "此插件";
export const SUBJECT_PLURAL = "部分插件";

/** 「运行环境缺失」是另一种事。
 *
 * 现场：没装 Node 的机器上，npx 型 MCP 连不上，被记成"连不上"，这一屏说
 * 「不兼容 / 不支持 / 已被 Crannia 屏蔽」。而真相是这台机器少了个程序，
 * 装上就好 —— 两件事的做法完全相反（一个去装东西，一个去找作者）。
 */
export const RUNTIME_CODE = "mcp_missing_runtime";

export function isRuntimeMissing(entry) {
  return String(entry?.code || "") === RUNTIME_CODE;
}

/** 这一批该怎么讲：`runtime` / `incompatible` / `mixed`。
 *
 * `mixed` 不许被压成另外两种之一 —— 一批里既有"缺环境"又有"真不兼容"时，
 *   用其中一种的口气盖住另一种，就是在替另一半说谎。
 */
export function noticeKind(entries) {
  const list = entries || [];
  const n = list.filter(isRuntimeMissing).length;
  if (!list.length) return "incompatible";
  if (n === list.length) return "runtime";
  return n === 0 ? "incompatible" : "mixed";
}

/** 从 `detail` 里抠出缺的那个命令（后端写的是 `command=npx`，纯 ASCII）——
 *  这样界面才能用 `t()` 拼一句当前语言的话，而不是直接显示后端那句中文。
 *  抠不到，空串（调用方回落到 `reason`）。
 */
export function missingCommand(entry) {
  const m = /command=([^\s"']+)/.exec(String(entry?.detail || ""));
  return m ? m[1] : "";
}

/** 一条账目该怎么显示 —— 用的就是 `Diagnosis` 那两个字段（`skill` / `path`）。
 *
 * 现在多带三样：`reason`（后端原话）· `runtime`（是不是缺环境）·
 *   `command`（缺哪个可执行文件）。
 * 缺了就说「（未提供）」，不打印 `undefined`（同 `skills-view.js` 那条判据）。
 */
export function noticeText(entry) {
  const name = entry?.name || entry?.owner || t("（未提供）");
  const path = entry?.path || t("（未提供）");
  return {
    name,
    path,
    reason: String(entry?.reason || ""),
    runtime: isRuntimeMissing(entry),
    command: missingCommand(entry),
  };
}

/** 正文主语：恰好一条，`此插件`；两条以上，`部分插件`。
 *
 * 空数组也回单数（它压根不会渲染 —— `App.jsx` 那边 `length > 0` 才挂）。
 */
export function subjectWord(entries) {
  return t(((entries || []).length > 1) ? SUBJECT_PLURAL : SUBJECT_SINGULAR);
}

/** 冲突栏那几格 —— 按原序，一条一份。 */
export function noticeFacts(entries) {
  return (entries || []).map(noticeText);
}

/** 音频那一格的依赖键：这一批换了才重播。
 *
 * 不能拿"第 0 条"当键 —— 队列里换掉头一条之后 `entries[0]` 可能没变，
 *   于是新来的那批一声不响（而"没声音"正是这条弹窗最容易出的那种无声故障）。
 */
export function noticeKey(entries) {
  return (entries || []).map((e) => (e && e.owner) || "").join("|");
}
