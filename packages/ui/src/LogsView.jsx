// 运行日志的独立页（设置弹窗里的子页，形状同 `MemoryLibrary`）。
//
// 数据走现成的 `GET /api/logs?lines=N` —— 服务端的环形缓冲（进程内、
//   最多 `_MAX_LOG_LINES` 行）。这一层只负责摆与导出，不自己攒日志。
//
// 导出是浏览器侧生成的文件（Blob + `<a download>`）：日志内容不上传、
//   也不经过后端 —— 与"这是本机排障数据"的定位一致。
import React, { useEffect, useState } from "react";
import { t } from "./i18n.js";

/** 一次拉多少行（服务端还会按自己的上限截一次）。 */
const LINES = 500;

export function LogsView({ client, notify, onBack }) {
  const [logs, setLogs] = useState(null);       // null = 还没读到
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function load() {
    setBusy(true);
    setErr("");
    try {
      const d = await client.call(`/api/logs?lines=${LINES}`);
      setLogs(Array.isArray(d && d.logs) ? d.logs : []);
    } catch (e) {
      //:读不到要说出来，不许伪装成"没有日志"（两者长得一样，而前者是故障）。
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { load(); /* eslint-disable-line */ }, []);

  function exportTxt() {
    const text = (logs || []).join("\n");
    if (!text) { notify(t("还没有日志可导出。")); return; }
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    const blob = new Blob([text + "\n"], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `crannia-logs-${stamp}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return (
    <div className="mem-page">
      <div className="mem-head">
        <button className="mem-back" onClick={onBack}>{t("← 返回设置")}</button>
        <h2>{t("运行日志")}</h2>
        <div className="log-acts">
          <button className="skill-btn" onClick={load} disabled={busy}>{t("刷新")}</button>
          <button className="skill-btn" onClick={exportTxt}
                  disabled={busy || !logs || !logs.length}>{t("导出")}</button>
        </div>
      </div>
      <div className="log-view">
        {err ? <p className="warn">{t("读不到日志：")}{err}</p> : null}
        {!logs ? <p className="muted">{t("正在读取…")}</p>
          : logs.length ? <pre>{logs.join("\n")}</pre>
            : <p className="muted">{t("还没有日志。")}</p>}
      </div>
    </div>
  );
}
