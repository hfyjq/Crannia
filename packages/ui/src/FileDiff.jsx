// 一个文件的「本轮改动」浮窗（居中，大小同设置）。
//
// 数据来自 `GET /api/file-diff`：会话键由服务端按 `?group=` 推，
// 这里只交路径与版本号。整份 diff 太长时按 hunk 边界截断，
// 末尾那句话把总数与"还有没显示的"一起说清楚。
import React, { useEffect, useState } from "react";
import { jsonRequest } from "./api.js";
import { t } from "./i18n.js";

//: 一次最多摆这么多 —— 先到者为准。
const MAX_LINES = 800;
const MAX_CHARS = 120000;

/** 按 hunk 边界截断：宁可少显示一整段，也不在半段中间断掉。 */
function clip(lines) {
  let chars = 0;
  let lastHunk = 0;
  for (let i = 0; i < lines.length; i++) {
    chars += lines[i].length + 1;
    if (i + 1 > MAX_LINES || chars > MAX_CHARS) {
      return { shown: lines.slice(0, lastHunk > 0 ? lastHunk : i), truncated: true };
    }
    if (lines[i].startsWith("@@")) lastHunk = i;
  }
  return { shown: lines, truncated: false };
}

function clsOf(ln) {
  if (ln.startsWith("+")) return "diff-line add";
  if (ln.startsWith("-")) return "diff-line del";
  return "diff-line";
}

/** `@@ -3,4 +3,4 @@` -> `⋯ 第 3 行起`。多段 diff 时它是分界，别整个抹掉。 */
function hunkStart(ln) {
  const m = ln.match(/\+(\d+)/);
  return m ? Number(m[1]) : 0;
}

export function FileDiff({ path, name, version, group, client, onClose }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    let alive = true;
    const q = "?path=" + encodeURIComponent(path) + "&version=" + encodeURIComponent(version)
      + (group ? "&group=" + encodeURIComponent(group) : "");
    client.call("/api/file-diff" + q)
      .then((d) => { if (alive) setData(d || {}); })
      .catch((e) => { if (alive) setErr(e.message || String(e)); });
    return () => { alive = false; };
  }, [path, version, group]);

  function reveal() {
    client.call("/api/reveal", jsonRequest("POST", { path }))
      .catch((e) => setErr(e.message || String(e)));
  }

  // 文件头那两行（`--- 改前 x` / `+++ 改后 x`）是重复的 —— 标题就是文件名。
  //   只按位置剥前两行（`--- ` 也可能是正文里被删掉的一行）。
  const raw = (data && data.diff) ? String(data.diff).split("\n") : [];
  const lines = (raw[0] || "").startsWith("--- ") && (raw[1] || "").startsWith("+++ ")
    ? raw.slice(2) : raw;
  const { shown, truncated } = clip(lines);
  const kind = data && data.kind;

  return (
    <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <section className="settings mem-shell diff-shell">
        <div className="mem-page">
          <div className="mem-head">
            <button className="mem-back" onClick={onClose}>{t("返回")}</button>
            <h2>{name || path}</h2>
            <div className="log-acts">
              <button className="skill-btn" onClick={reveal}>{t("在资源管理器中显示")}</button>
            </div>
          </div>

          <div className="diff-body">
            {err ? (
              <p className="diff-note">{t("读取失败：")}{err}</p>
            ) : !data ? (
              <p className="muted">{t("读取中…")}</p>
            ) : kind === "new" ? (
              <p className="diff-note">
                {t("这是本轮新建文档，")}
                <button className="link" onClick={reveal}>{t("在资源管理器中显示")}</button>
                {data.added != null ? "（+" + data.added + "）" : null}
              </p>
            ) : kind === "diff" || kind === "gone" ? (
              <>
                {data.note ? <p className="diff-note">{t(data.note)}</p> : null}
                <pre className="diff-view">
                  {shown.map((ln, i) => (
                    ln.startsWith("@@")
                      ? <div key={i} className="diff-line hunk">
                          {t("⋯ 第 {n} 行起", { n: hunkStart(ln) })}
                        </div>
                      : <div key={i} className={clsOf(ln)}>{ln === "" ? " " : ln}</div>
                  ))}
                </pre>
                {truncated ? (
                  <p className="diff-note">
                    {t("共计 +{a}/−{b}，仍有未显示完全的改动，", { a: data.added, b: data.removed })}
                    <button className="link" onClick={reveal}>{t("在资源管理器中显示")}</button>
                  </p>
                ) : null}
              </>
            ) : (
              <p className="diff-note">
                {data.note ? t(data.note) : t("拿不到这个文件的改动")}
                {" "}
                <button className="link" onClick={reveal}>{t("在资源管理器中显示")}</button>
              </p>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
