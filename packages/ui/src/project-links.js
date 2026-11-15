// 项目对外链接 —— 一处定义，一个口径一个地方。
//
// 用户给定仓库地址，并且当天定了一条：
// 「把现在的所有反馈按钮全部取消邮箱，改成引导至 github 的 issue 界面。
//   拒绝泄露一个可能的攻击点和隐私问题。」
//
// 为什么这件事比"换个链接"重要：
// 旧的 `/api/feedback` 是拿用户自己配的邮箱账号（`mail_parser` skill）
// 把正文 + 导出的运行日志发出去 —— 那是一条真实的外发通道，
// 而且它要求用户先把邮箱凭据填进来才用得成。
// 所以删掉它，同时没有引入任何新的外发面（`window.open` 由系统浏览器接管）。

/** 项目主页。 */
export const PROJECT_URL = "https://github.com/hfyjq/Crannia";

/** 问题反馈：直接进新建 issue（GitHub 会先给模板选择页）。 */
export const ISSUES_URL = PROJECT_URL + "/issues/new/choose";

/** 在系统浏览器里打开一条外链。
 *
 *  与 `Markdown.jsx` 同一条路（那边是正文里的链接）：`window.open`
 *    交给宿主去开，失败也不许把界面弄崩 —— 返回 `false` 让调用方去提示。
 *  不在这里拼 URL、也不在这里判断 `web` / 桌面壳：
 *    Electron 的 `setWindowOpenHandler` 会把 http(s) 交给系统浏览器（见 `apps/desktop`）。
 */
export function openExternal(url) {
  try {
    window.open(url, "_blank", "noopener,noreferrer");
    return true;
  } catch (e) {
    return false;
  }
}
