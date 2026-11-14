// 对外说明页 —— 正文是仓库里的 `docs/help/*.md`，构建时由 Vite 内联
// （`?raw`），改了 .md 要重新 `npm run build` 才生效。
//
// 正文不进 i18n 短句表（同 `Skills.jsx` 的 `setup_guide`）：那是整篇说明，
//    不是界面短句。每篇两份文件：`<名字>.md`（中文）与 `<名字>.en.md`（英文）。
// 正文里的数字不手写：写 `{breaker_consecutive}` 这类占位，
//    由 `HELP_NUMBERS` 替换（读者 `tests/test_help_docs.py` 钉着它与后端常量同值）。
// 这是用户可见的文档：说话要像人话，不写内部符号名。
import { t, lang } from "./i18n.js";
import { Markdown } from "./Markdown.jsx";
import { HELP_NUMBERS } from "./help-numbers.js";

import { HELP_TEXT } from "./help-content.js";

/** 说明页清单 —— 设置页的「帮助文档」与两处弹窗里的链接共用这一张。
 *  `id` 是两处链接的目标（`onOpenDoc("auto")`）：拼错 = 点了没反应。
 *  正文从 `help-content.js` 来（那个文件不登记在 i18n 扫描表里 —— 见它的抬头）。 */
export const HELP_DOCS = [
  { id: "auto", title: "自动模式说明", ...(HELP_TEXT.auto || {}) },
  { id: "takeover", title: "接管模式说明", ...(HELP_TEXT.takeover || {}) },
];

/** 取一篇的正文，并把 `{数字}` 占位换掉。 */
export function helpText(doc) {
  const src = String((doc && (doc[lang()] || doc.zh)) || "");
  return src.replace(/\{([a-z_]+)\}/g, (m, k) =>
    (Object.prototype.hasOwnProperty.call(HELP_NUMBERS, k) ? String(HELP_NUMBERS[k]) : m));
}

/** 说明页本体（不带外壳）—— 调用方决定把它放进哪个壳：
 *  · 设置弹窗里的子页：`<section className="settings mem-shell">`（同记忆库/日志页）
 *  · 安全弹窗里点「返回」时：外层再套一层 `.backdrop`，点空白直接退出 */
export function HelpPanel({ docId, onBack }) {
  const doc = HELP_DOCS.find((d) => d.id === docId);
  if (!doc) return null;      // 未知 id，什么都不画（不编一个空页）
  return (
    <div className="mem-page help-page">
      <div className="mem-head">
        <button className="mem-back" onClick={onBack}>{t("← 返回")}</button>
        <h2>{t(doc.title)}</h2>
      </div>
      <div className="help-body">
        <Markdown text={helpText(doc)} />
      </div>
    </div>
  );
}
