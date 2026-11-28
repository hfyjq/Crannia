// 代码块复制按钮的纯逻辑（JSX 组件里的那部分下沉到这里，node 可直接测）。
//
// 为什么要拆：Markdown.jsx 是 JSX，本包没有 JSX 测试设施；而"复制成功说什么、
// 失败说什么、什么时候回到初始文案"这件事值得钉住 —— 失败那条尤其不能
// 跟成功长得一样（与消息级复制按钮同一条纪律，见 App.jsx 的 copyNote）。

import { t } from "./i18n.js";

/** 按钮上该显示的文案。`state` 是组件记住的那一次动作结果。 */
export function codeCopyLabel(state) {
  if (state === "ok") return t("已复制");
  if (state === "fail") return t("复制失败");
  return t("复制代码");
}

/** 失败时的悬停提示：给一条不经过渲染进程的退路（壳的原生右键复制）。 */
export function codeCopyTitle(state) {
  return state === "fail" ? t("换个方式：复制整条回复后手动取用") : t("复制代码");
}

/** 状态多久回落到初始文案（毫秒）。与消息级按钮的 1600 对齐。 */
export const CODE_COPY_RESET_MS = 1600;
