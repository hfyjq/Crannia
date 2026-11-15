// 更新日志的独立页（设置弹窗里的子页，形状同 `LogsView`）。
//
// 数据走 `GET /api/update/log` —— 后端读的是磁盘上的
//   `data/updates/update.log`（检查失败 / 下载重试 / 安装失败都在里面）。
// 判据：失败必须读得到。2026-11-14 真机那次"下载断、装 13 分钟、
//   不知道好没好"，用户侧一个字都没有 —— 这一页就是补那一段。
import React, { useEffect, useState } from "react";
import { t } from "./i18n.js";

export function UpdateLogView({ client, notify, onBack }) {
  const [text, setText] = useState(null);       // null = 还没读到
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function load() {
    setBusy(true);
    setErr("");
    try {
      const d = await client.call("/api/update/log");
      setText(String((d && d.log) || ""));
    } catch (e) {
      //: 读不到要说出来，不许伪装成"没有日志"（两者长得一样，而前者是故障）。
      setErr(e.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { load(); /* eslint-disable-line */ }, []);

  function exportTxt() {
    if (!text) { notify(t("还没有日志可导出。")); return; }
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `crannia-update-${stamp}.log`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return (
    <div className="mem-page">
      <div className="mem-head">
        <button className="mem-back" onClick={onBack}>{t("← 返回设置")}</button>
        <h2>{t("更新日志")}</h2>
        <div className="log-acts">
          <button className="skill-btn" onClick={load} disabled={busy}>{t("刷新")}</button>
          <button className="skill-btn" onClick={exportTxt}
                  disabled={busy || !text}>{t("导出")}</button>
        </div>
      </div>
      <div className="log-view">
        {err ? <p className="warn">{t("读不到日志：")}{err}</p> : null}
        {text === null ? <p className="muted">{t("正在读取…")}</p>
          : text ? <pre>{text}</pre>
            : <p className="muted">{t("还没有更新日志。")}</p>}
      </div>
    </div>
  );
}
