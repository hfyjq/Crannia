// 说明页正文的装载处 —— 只做一件事：把 `docs/help/*.md` 以原文串带进构建。
//
// 为什么单独一个文件：`scripts/i18n_scan.py` 会把登记过的 `.jsx` 里
//    每一个字符串字面量当界面文案收进工作表，而 `?raw` 的导入路径也是字符串
//    所以混进四行"../../..docs/help/..."的假文案。
//    本文件不登记在扫描表里，这四条路径不会被当成文案。
// 这里不许写 `t()`：文案在外壳（`HelpPanel.jsx`）里。
import autoZh from "../../../docs/help/auto.md?raw";
import autoEn from "../../../docs/help/auto.en.md?raw";
import takeoverZh from "../../../docs/help/takeover.md?raw";
import takeoverEn from "../../../docs/help/takeover.en.md?raw";

/** id -> {zh, en}。id 与 `HelpPanel.jsx::HELP_DOCS` 对齐（读者钉着）。 */
export const HELP_TEXT = {
  auto: { zh: autoZh, en: autoEn },
  takeover: { zh: takeoverZh, en: takeoverEn },
};
