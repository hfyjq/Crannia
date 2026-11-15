import React, { useEffect, useState } from "react";
import { Check, ChevronDown, ChevronRight, Loader2, TriangleAlert, X } from "lucide-react";

// 活动块 —— 详细事件流。
//
// 它是 `messages` 里的一个条目（`role:"activity"`），不是「浮在对话流底部的一个元素」。
//   判据：执行完成后，步骤流的框要缩回一个小尺寸显示已完成，
//   回复另开一个标准尺寸的框 —— 一个浮在底部的元素排不到「问」与「答」之间
//   （它会跑到回复下面去），所以必须进消息流。
//
// 两种形态：
//   · `data.live` —— 正在跑：完整尺寸，带转轮 / 当前这一步 / 用时 / 打断
//   · `data.done` —— 缩回小条：`✓ 已完成 · N 步 · 12.3s`，点一下展开看步骤

import { t } from "./i18n.js";
//:草稿与「中途说的话」都按 Markdown 渲染 —— 与最终回复同一个渲染器。
//:判据：同一段文字，草稿与正文必须长得一样（草稿只是更早出现）。
//:   直出的后果：模型回一张表格时，草稿是原始 Markdown
//:   （竖线 + percent 编码的长路径），像一屏代码、比正文高得多，
//:   正文一出现就像"突然消失"。
import { Markdown } from "./Markdown.jsx";
//:一步的用时：< 1s 给毫秒，≥ 1s 换成秒（用户）。
//:判据住在 `format.js`（纯函数，node 有断言），这里只管调。
import { fmtStepMs } from "./format.js";
function fmt(ms) {
  if (ms < 0 || !isFinite(ms)) return "";
  return ms < 60000
    ? (ms / 1000).toFixed(1) + "s"
    : Math.floor(ms / 60000) + "m" + Math.floor((ms % 60000) / 1000) + "s";
}

export function ActivityBlock({ data, onCancel, open = false, onToggle }) {
  const live = !!data.live;
  const [, tick] = useState(0);

  //:只有活着的时候才走表 —— 完结之后用时是冻结的（`endedAt - startedAt`），
  //: 让它继续跳就是在撒一个"它还在跑"的谎。
  useEffect(() => {
    if (!live) return undefined;
    const timer = setInterval(() => tick((n) => n + 1), 100);
    return () => clearInterval(timer);
  }, [live]);

  //:展开/收起是受控的（`open` + `onToggle` 由 App 给）：追问时 App
  //:`setOpenAct("")` 一次性把所有旧框收起来（用户）。
  //:以前这一格是组件自己的 state + 一个"请收起"信号 —— 信号某次没送到，
  //:它就静静地不折，而且从外面看不出来。

  //:整块用时 —— 只在「已完成」那条小条上（`12.3s`），与 `N 步` 并排。
  const ms = (live ? Date.now() : (data.endedAt || Date.now())) - data.startedAt;
  //:头部显示的是当前步骤 + 当前步骤用时。
  //:头部那个数是这一步的（思考中 / 正在执行命令各自从头算）——
  //:   整块的用时显示在发送框上那条横幅（`StatusBar`），这里不重复它。
  //:`stepStartedAt` 缺失时退回整块起点（不是 0 —— `Date.now() - 0`
  //:   会显示成一个 5 位数的秒数，看起来像个真读数）。
  const stepMs = Date.now() - (data.stepStartedAt || data.startedAt);
  const lines = data.lines || [];
  const steps = lines.filter((l) => l.kind === "step").length;
  const body = (
    <div className="activity-body">
      {lines.map((line, i) =>
        line.kind === "speech" ? (
          // 模型中途说的话 —— 在框内，且不带动作条
          //   （动作条只挂在「明确的对用户回复」上）。
          // 按 Markdown 渲染（与最终回复同一个渲染器，判据见文件头的 import）。
          <div className="activity-speech" key={line.id || i}>
            <Markdown text={line.text} />
          </div>
        ) : (
          <div className="activity-step" key={line.id || i}>
            <i className={line.ok === false ? "bad" : line.ok === true ? "good" : "run"} />
            <div className="activity-step-main">
              <span className="activity-step-name">{line.text}</span>
              {/* 参数那一行：单行、省略号，悬停看全文（`title`）。
                  命令原文以前不显示（`arg_hint` 只取 basename，会把危险的那半藏掉）；
                  现在走 `sub_hint` = 整条命令（用户）。 */}
              {line.sub ? (
                <code className="activity-step-sub" title={line.subFull || line.sub}>
                  {line.sub}
                </code>
              ) : null}
            </div>
            <span className="activity-step-ms">{fmtStepMs(line.ms)}</span>
          </div>
        )
      )}
      {data.draft ? (
        <div className="activity-draft"><Markdown text={data.draft} /></div>
      ) : null}
    </div>
  );

  // ── 重试没救回来，红色小条常驻（不许装成"已完成"）──
  //   这一支必须在"已完成"之前，
  //   否则重试耗尽之后它会被画成一个绿色的 ✓（`task.end.ok` 那边已经是 false）。
  //:失败的一轮不许显示成绿色「已完成」。
  //:重试的红横幅在发送框上方那条（`StatusBar`），不在这里 ——
  //:   对话流里另起一块是错的。
  //:这一块只在这一轮真的失败之后变成红色小条，说明"没完成"。
  if (!live && data.failed) {
    return (
      <section className={"activity done retry" + (open ? " open" : "")}>
        <header className="activity-head as-row" onClick={() => onToggle && onToggle()}>
          <span className="activity-bad"><TriangleAlert size={13} /></span>
          <b>{t("未完成")}</b>
          <span className="activity-sum">{t("这一轮没有完成 —— 详情见下面的说明")}</span>
          <span className="activity-toggle">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
        </header>
        {open ? body : null}
      </section>
    );
  }

  // ── 已完成：缩回小条 ──
  if (!live) {
    return (
      <section className={"activity done" + (open ? " open" : "")}>
        <header className="activity-head as-row" onClick={() => onToggle && onToggle()}>
          <span className="activity-ok"><Check size={13} /></span>
          <b>{t("已完成")}</b>
          <span className="activity-sum">{steps ? t("{steps} 步 · ", { steps }) : ""}{fmt(ms)}</span>
          <span className="activity-toggle">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</span>
        </header>
        {open ? body : null}
      </section>
    );
  }

  // ── 进行中：完整尺寸 ──
  return (
    <section className="activity">
      <header className="activity-head">
        <span className="activity-spin"><Loader2 size={15} /></span>
        {/* 这里是当前这一步（「思考中」/「正在执行命令」），
            由 `App.jsx` 在每个 `tool.start` / `tool.end` 上翻 —— 不是一个写死的词。
            具体是哪个工具在下面那一行（`lines` 里的 `step`），这里只说阶段。 */}
        <b>{data.stepLabel || t("正在执行")}</b>
        <span className="activity-elapsed">{fmt(stepMs)}</span>
        <button className="activity-cancel" onClick={onCancel} title={t("打断这一轮")}>
          <X size={13} />{t("打断")}
        </button>
      </header>
      {/* Ctrl+B 提示—— 只在真有可转的前台任务时出现。
          判据是 `data.backgroundable`，它由 `tool.start.backgroundable` 维护
          （服务端说这一步有没有前台执行体，前端不按工具名猜）。
          它跟着当前步骤走：步骤结束 / 转后台之后 `tool.end` 一来就消失。 */}
      {data.backgroundable > 0 ? (
        <p className="activity-tip">{t("tips：ctrl+B 将当前任务转至后台")}</p>
      ) : null}
      {body}
    </section>
  );
}
