import React, { useCallback, useEffect, useState } from "react";
import { ListChecks } from "lucide-react";

// 任务清单那一栏 —— 把模型的任务清单渲染成右缘抽屉里的一栏
// （`Rail.jsx` 的 `PANELS`）。载体是 `core/todos.py` + 工具回执。
//
// ## 三条判据（与 `core/todos.py` 同源，这里只是画）
//
// 1. 它是只读的第二个读者。清单的写者只有一个 —— `todo_write` 工具。
//    这一屏不写、不建、不碰任何状态（同 `StarsPanel` 那条"一个字节都不写"）。
// 2. 状态是后端给的，前端不重算 —— `pending`/`in_progress`/`completed` 的
//    记号与配色照 `core/todos.py::STATUS_MARKS` 那一套（`[ ]` / `[~]` / `[x]`）。
//    不在这里自己判"哪一项该算进行中"：那是 `todo_write` 的校验管的。
// 3. "没有清单"与"没读到"是两件事 —— 读失败不许塌成
//    「还没有任务清单」那一句（后者看起来像一个结论）。

/** 三个状态各自的记号与类名。与 `core/todos.py::STATUS_MARKS` 逐字同源 ——
 *  改那边就要改这边（`[ ]` / `[~]` / `[x]`）。 */
import { t } from "./i18n.js";
const MARKS = { pending: "[ ]", in_progress: "[~]", completed: "[x]" };

export function TodosPanel({ client }) {
  //:`null` = 还没读到（与"读到了、但清单是空的"分得开）。
  const [items, setItems] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    setErr("");
    try {
      const d = await client.call("/api/todos");
      setItems(d.todos || []);
    } catch (e) {
      setErr(e.message || String(e));
      setItems([]);
    } finally {
      setBusy(false);
    }
  }, [client]);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="todos">
      <div className="todos-head">
        <b>{t("本会话的任务清单")}</b>
        <button className="todos-refresh" onClick={load} disabled={busy}>
          {busy ? t("读取中…") : t("刷新")}
        </button>
      </div>
      {/* 读失败先说读失败 —— 排在"清单是空的"前面（判据 3）。 */}
      {err && <p className="todos-err">{t("读不到任务清单：{err}", { "err": err })}</p>}
      {items === null && <p className="muted">{t("读取中…")}</p>}
      {items !== null && !err && !items.length && (
        <p className="muted">
          {t("这一段还没有任务清单 —— 模型在做多步任务时会用")} <code>todo_write</code> {t("建一份。")}
        </p>
      )}
      {(items || []).map((it, i) => (
        <div className={"todo-row todo-" + it.status} key={i}>
          <i className="todo-mark">{MARKS[it.status] || "?"}</i>
          <span className="todo-text">{it.content}</span>
        </div>
      ))}
      {!!(items || []).length && (
        <p className="todos-note">
          <ListChecks size={12} /> {t("这一栏")}<b>{t("只读")}</b> {t("—— 改它的是模型自己那个")}{" "}
          <code>todo_write</code> {t("工具。")}
        </p>
      )}
    </div>
  );
}
