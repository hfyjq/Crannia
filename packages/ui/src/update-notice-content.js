// 更新公告正文的装载处 —— 把 `docs/updates/*.txt` 以原文串带进构建。
//
// 为什么单独一个文件（与 `help-content.js` 同一条）：`scripts/i18n_scan.py`
//    会把登记过的 `.jsx` 里每一个字符串字面量当界面文案收进工作表，
//    而这里的导入路径也是字符串 —— 混进去就是几条假文案。
//    本文件不登记在扫描表里。
//
// 用 glob 而不是一条条 `import`：发版时只要在 `docs/updates/` 里丢一份新 txt，
//    不用回来改这张表（改了忘改就得等下个版本才发现）。
//
// 两种语言成对：`v1.0.0.txt`（中文）+ `v1.0.0.en.txt`（英文）。
//    英文那份缺了就回落中文 —— 与全仓 `t()` 的回落同一条判据（永不空白）。
import { lang } from "./i18n.js";
import { compareNoticeId, parseNotice } from "./update-notice.js";

const _raw = import.meta.glob("../../../docs/updates/*.txt", {
  query: "?raw", import: "default", eager: true,
});

/** `[{id, zh, en}]` —— id 是文件名去掉 `.txt`（以及 `.en`）。 */
export const UPDATE_NOTICES = (() => {
  const byId = new Map();
  for (const [path, text] of Object.entries(_raw)) {
    const name = String(path).split("/").pop().replace(/\.txt$/, "");
    const isEn = name.endsWith(".en");
    const id = isEn ? name.slice(0, -3) : name;
    const rec = byId.get(id) || { id, zh: "", en: "" };
    if (isEn) rec.en = String(text || "");
    else rec.zh = String(text || "");
    byId.set(id, rec);
  }
  return Array.from(byId.values());
})();

/** 这一份在当前界面语言下的正文（英文缺了就用中文）。 */
export function noticeText(rec) {
  if (!rec) return "";
  return (lang() === "en" && rec.en) ? rec.en : rec.zh;
}

/** 最新那一份，已经按当前语言解析好；没有就 `null`。 */
export function latestParsed() {
  const items = UPDATE_NOTICES.slice().sort((a, b) => compareNoticeId(a.id, b.id));
  const last = items[items.length - 1];
  if (!last) return null;
  const parsed = parseNotice(noticeText(last));
  if (!parsed.title) return null;
  return { id: last.id, ...parsed };
}
