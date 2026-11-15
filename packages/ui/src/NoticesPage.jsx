// 往期公告 —— 设置页「其他设置 → 往期公告」点开的那一页。
//
// 与记忆库同一套外壳：`.mem-page` + `.mem-head` + 左上角返回（设置弹窗整块换掉，
// 不是再叠一层浮窗）。
//
// 内容与启动时那张弹窗同源（`docs/updates/*.txt`，构建时打进包里）：
//   这里是全部，按 id 从新到旧；每一条默认只展开最新那份（`<details open>`），
//   老的收起来 —— 用户多半只想看"这一版改了什么"，要翻历史再点开。
import { useMemo, useState } from "react";
import { t } from "./i18n.js";
import { compareNoticeId, parseNotice } from "./update-notice.js";
import { noticeText, UPDATE_NOTICES } from "./update-notice-content.js";

/** 一条公告的可搜文本：标题 + id + 全部条目（含小节头）。
 *  只搜"看得见的东西"—— 用户搜的是版本号或某句改动，不是文件路径。 */
function searchText(n) {
  const parts = [n.title, n.id];
  for (const sec of n.sections) {
    if (sec.head) parts.push(sec.head);
    parts.push(...sec.items);
  }
  return parts.join(" ").toLowerCase();
}

export function NoticesPage({ onBack }) {
  const [q, setQ] = useState("");
  const items = useMemo(() => (UPDATE_NOTICES || [])
    .map((n) => ({ id: n.id, ...parseNotice(noticeText(n)) }))
    .filter((n) => n.title)
    .sort((a, b) => compareNoticeId(b.id, a.id)), []);

  const needle = q.trim().toLowerCase();
  //: 搜到的那几条全部展开 —— 命中通常在正文里，折叠着等于让用户再点一次。
  const shown = needle
    ? items.filter((n) => searchText(n).indexOf(needle) >= 0)
    : items;

  return (
    <div className="mem-page">
      <div className="mem-head">
        <button className="mem-back" onClick={onBack}>{t("← 返回")}</button>
        <h2>{t("往期公告")}</h2>
      </div>
      <div className="notices-search">
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t("搜索版本号或关键词，例如 v1.0.1")}
          aria-label={t("搜索公告")}
        />
      </div>
      <div className="help-body">
        {!items.length ? (
          <p className="skill-panel-note">{t("还没有往期公告。")}</p>
        ) : !shown.length ? (
          <p className="skill-panel-note">{t("没有匹配的公告。")}</p>
        ) : shown.map((n, i) => (
          <details key={n.id} className="notices-item"
                   open={!!needle || i === 0}>
            <summary>{n.title}</summary>
            <div className="notices-body">
              {n.sections.map((sec, j) => (
                <section key={(sec.head || "") + j} className="update-notice-sec">
                  {sec.head ? <h3>{sec.head}</h3> : null}
                  <ol>
                    {sec.items.map((it, k) => <li key={k}>{it}</li>)}
                  </ol>
                </section>
              ))}
            </div>
          </details>
        ))}
      </div>
    </div>
  );
}
