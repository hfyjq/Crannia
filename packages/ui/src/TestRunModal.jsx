import React, { useEffect, useRef, useState } from "react";
import { Loader2, X } from "lucide-react";
import { jsonRequest } from "./api.js";
import { Markdown } from "./Markdown.jsx";

// 「试跑」= 一个临时浮窗，就是一个临时对话：
// 把任务丢给 agent，浮窗只显示 agent 的步骤流和结果，关闭时对话清掉。
//
// 它保证的事：试跑不跑在用户真正的对话里 ——
//   否则主对话会多出一段用户从没说过的话（框架注入的正文），跑十次就是十条。
//
// 不带上下文：只要看定时任务执行的效果
//   所以独立段、空历史。代价如实记：到点那天它是有历史的，
//   所以这里的"效果"与真跑那天可能不完全一样。
//
// 怎么把"这一次"的步骤挑出来：`tool.start` 事件不带 `job_id`
//   所以只能拿 `cron.start` / `cron.end`（带 `job_id`）当括号圈住一段。
//   同一会话里若同时还有用户自己的轮次在跑，它的步骤也会落进括号里
//   （已知的粗粒度；浮窗是看效果用的，不是审计）。

import { t } from "./i18n.js";
export function TestRunModal({ job, client, onClose }) {
  const [steps, setSteps] = useState([]);
  const [text, setText] = useState("");
  const [err, setErr] = useState("");
  const [phase, setPhase] = useState("running");     // running | done | failed
  const cancelled = useRef(false);
  const storageId = useRef("");

  async function discard(sid) {
    if (!sid) return;
    try { await client.call("/api/cron/run/close", jsonRequest("POST", { storage_id: sid })); }
    catch { /* 删不掉就别嚷嚷：它是一段框架造的临时对话，不是用户的数据 */ }
  }

  useEffect(() => {
    cancelled.current = false;
    let inside = false;
    const offs = [
      client.subscribe("cron.start", (d) => {
        if (d && d.job_id === job.job_id && d.trigger === "manual") inside = true;
      }),
      client.subscribe("tool.start", (d) => {
        if (!inside) return;
        const line = (d && (d.activity || d.name)) || "";
        if (line) setSteps((s) => (s[s.length - 1] === line ? s : s.concat([line])));
      }),
      client.subscribe("cron.end", (d) => {
        if (d && d.job_id === job.job_id && d.trigger === "manual") inside = false;
      }),
    ];

    (async () => {
      try {
        const out = await client.call("/api/cron/run", jsonRequest("POST", { job_id: job.job_id }));
        storageId.current = (out && out.storage_id) || "";
        if (cancelled.current) { await discard(storageId.current); return; }
        if (out && out.text) setText(out.text);
        setPhase(out && out.ok === false ? "failed" : "done");
        if (out && out.ok === false) setErr(out.error || out.reason || t("未成功运行"));
      } catch (e) {
        if (cancelled.current) return;
        setPhase("failed");
        setErr(e && e.message ? e.message : String(e));
      }
    })();

    return () => {
      cancelled.current = true;
      offs.forEach((off) => { try { off(); } catch { /* 已经断了 */ } });
    };
  }, [client, job.job_id]);

  async function close() {
    // 还在跑就关，这一轮继续跑完，但结果不留（关的那一刻就去删那一段）。
    //    所以不阻塞用户，也不留一段没人认领的临时对话。
    const sid = storageId.current;
    cancelled.current = true;
    if (sid) await discard(sid);
    onClose();
  }

  return (
    <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <section className="cron-run">
        <div className="settings-head">
          <div>
            <small>{t("试跑")}</small>
            <h2>{job.title}</h2>
          </div>
          <button className="round" onClick={close} title={t("关掉并清掉这次试跑的对话")}>
            <X size={18} />
          </button>
        </div>

        <p className="cron-run-note">
          {t("本次运行为试跑，与现有对话无关，关闭即清理。请注意本次运行效果并不完全代表实际运行效果")}
        </p>

        <div className="cron-run-steps">
          {steps.length ? steps.map((s, i) => (
            <div className="cron-run-step" key={i}><i />{s}</div>
          )) : (
            <div className="cron-run-step muted">
              {phase === "running" ? t("正在运行…（尚未产生第一步操作）") : t("此轮没有产生步骤")}
            </div>
          )}
        </div>

        <div className="cron-run-out">
          {phase === "running" && (
            <div className="cron-run-wait"><Loader2 size={15} className="spin" />{t("等待结果刷新")}</div>
          )}
          {phase === "failed" && <div className="preview bad">{err}</div>}
          {phase === "done" && (
            text ? <Markdown text={text} /> : <p className="muted">{t("已完成，未产出文字")}</p>
          )}
        </div>

        <footer>
          <span>{phase === "running" ? t("仍在运行中，此时关闭会导致结果丢失，且不会终止此次运行") : t("关掉即清掉这次试跑的对话。")}</span>
          <button onClick={close}>{phase === "running" ? t("关掉") : t("知道了")}</button>
        </footer>
      </section>
    </div>
  );
}
