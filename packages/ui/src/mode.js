// 交互模式轴在界面上的一处定义 + 一处判据 —— 纯函数，不认识 React。
//
// 为什么单独一个文件：本包没有 vitest/jest，而"写一条判据却不配测试"
//    不可接受，写成纯函数，`node` 直接跑断言
//    （`packages/ui/tests/mode.test.mjs` + `tests/test_desktop_mode.py`，
//     形状照 `context.js` 那一对）。
//
// 表与后端 `core/modes.py::_MODES` 同源（键 + 中文名照抄那一张）。
//   这是唯一的一份 —— 不要在组件或设置页里再内联一张
//      `[["manual","手动"], …]`（两份会漂）。

/** 四档模式（`key` + 界面名）—— 顺序即显示顺序。
 *
 *  第四档 `full_access` 只能在这里（= 对话里那枚 chip，
 *  走 `/api/mode` 按会话开）；设置页那个默认档不列它 ——
 *  判据在后端（`default_allowed`），设置页只负责过滤。
 */
export const MODE_LABELS = [
  { key: "manual", label: "手动" },
  { key: "auto", label: "自动" },
  { key: "plan", label: "计划" },
  { key: "full_access", label: "完全权限" },
];

/** 输入框那枚 chip 要显示的键（标签必须反映事实）。
 *
 * 判据 —— 显示这一段真正在跑的那个，按这个次序取：
 *   · `status.mode_session`    = 本会话已经冻结的（跑过一轮之后才有）
 *   · `status.mode_configured` = 还没冻结时它将要用的那个（= 设置里的默认值）
 *   · 两个都读不到，`manual`
 *
 * 不许退回读 `settings.default_mode`：
 *   那个 `settings` 对象只在打开设置弹窗时才拉（`App.jsx::openSettings`）
 *，启动时恒为 `null`，兜底永远落空，第一轮之前 chip 一律显示「手动」，
 *   而真实默认值可能是别的。结果是"对话前手动、对话后自动（用户没切过）"
 *   —— 而那个模式自始至终没变过，变的只是标签。
 */
export function chipMode(status) {
  const s = status || {};
  return s.mode_session || s.mode_configured || "manual";
}
