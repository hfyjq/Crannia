import React, { useEffect, useState } from "react";
import {
  emptyState, formatDuration, mergeRecords, preview, recordOk, recordTitle,
} from "./trace-view.js";

// 逐条轨迹 —— 用户看得见「调了什么 · 参数是什么 ·
// 它想了什么 · 结果是什么」。
//
// 三条判据（与 `core/trace.py` 同源）：
//   ① 默认关 —— 关着时这一屏说的是"去设置页打开"，不是一个空列表
//      （"没打开"与"没有记录"必须分得开）。
//   ② 按需拉（`GET /api/trace`，`since` 增量）—— 不进事件流，
//      所以刷新 / 重连只是再拉一次，不会留下幽灵记录。
//   ③ 这些字永远不回灌给模型；它们只是给用户看的。

import { t } from "./i18n.js";
export function TracePanel({ client }) {
  const [data, setData] = useState(null);
  const [rows, setRows] = useState([]);
  const [err, setErr] = useState("");

  async function load(since = 0) {
    try {
      const r = await client.call("/api/trace" + (since ? `?since=${since}` : ""));
      setData(r);
      setErr(r.error || "");
      setRows((old) => mergeRecords(old, r.records || []));
    } catch (e) {
      setErr(e.message);
    }
  }

  useEffect(() => { load(0); }, [client]);   // eslint-disable-line

  //:面板开着时每 2 秒增量拉一次（只拉新序号；与 `/api/context` 那条
  //:"开着才刷"同族）。关掉抽屉就卸载，定时器自动停。
  useEffect(() => {
    const timer = setInterval(() => {
      const last = rows.length ? Number(rows[rows.length - 1].seq) || 0 : 0;
      load(last);
    }, 2000);
    return () => clearInterval(timer);
  }, [client, rows.length]);   // eslint-disable-line

  const state = emptyState(data);

  return (
    <div className="trace-panel">
      <div className="trace-head">
        <span className="muted">
          {t("参数全文 · 模型的思考 · 工具结果")}
        </span>
        <button className="skill-btn" onClick={() => load(0)}>{t("刷新")}</button>
      </div>

      {err && <p className="muted">{err}</p>}

      {state === "off" && (
        <p className="trace-note">
          {t("没有打开「记录完整轨迹」。去")} <b>{t("设置 → 对话设置")}</b> {t("打开它， 下一轮开始就会记在这里。")}
        </p>
      )}
      {state === "empty" && (
        <p className="trace-note">{t("已经打开，这一轮还没有发生什么。")}</p>
      )}

      {data && data.dropped > 0 ? (
        <p className="trace-note">
          {t("只保留最近 200 条，更早的 {data.dropped} 条已不再显示。", { "data.dropped": data.dropped })}
        </p>
      ) : null}

      <div className="trace-list">
        {rows.map((r) => {
          const ok = recordOk(r);
          return (
            <div className={"trace-row" + (ok === false ? " trace-bad" : "")}
                 key={r.seq}>
              <div className="trace-row-head">
                <span className="trace-seq">#{r.seq}</span>
                <b className="trace-name">{recordTitle(r)}</b>
                {ok === false ? <span className="trace-badge">{t("失败")}</span> : null}
                {formatDuration(r.duration_ms)
                  ? <span className="trace-ms">{formatDuration(r.duration_ms)}</span>
                  : null}
              </div>

              {r.kind === "thinking" ? (
                <details className="trace-detail">
                  <summary>{preview(r.text)}</summary>
                  <pre>{r.text}</pre>
                </details>
              ) : (
                <>
                  <details className="trace-detail">
                    <summary>{t("参数：{v}", { v: preview(r.args) || t("（无）") })}</summary>
                    <pre>{r.args || t("（无）")}</pre>
                  </details>
                  <details className="trace-detail">
                    <summary>{t("结果：{v}", { v: preview(r.result) || t("（空）") })}</summary>
                    <pre>{r.result || t("（空）")}</pre>
                  </details>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
