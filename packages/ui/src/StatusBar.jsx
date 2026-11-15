import React, { useEffect, useRef, useState } from "react";
import { Loader2, TriangleAlert, X, Check } from "lucide-react";
//:那两个格式化器住在 `format.js`（纯函数，node 能直接断言；见那里头的判据）。
import { fmtCount, fmtDuration, retryCounter, retryLabel } from "./format.js";

// 发送框上方那条绿色横幅。
//
// 三样：
//   · 随机加载句 —— 来自 `loadingPhrases.js`（源头是仓库根的 `加载随机句.txt`），
//     每轮换一句（不是每个 token 换 —— 那会闪）。
//   · 本轮用时 —— 这一轮 LLM 往返走了多久。
//   · 本轮输出 token —— 「本轮输出」，不是会话累计。
//     数据来自 `turn.end` 的 `output_tokens`（`core/agent_loop.py`）。
//
// 它与「会话累计」（`task.end.tokens`，`Σ(prompt+completion)`）不是一个量 ——
//    两者差 99%（累计里绝大多数是每轮重发的输入）。别把这两个数换着用。
//
// ── 两个显示细节 ──────────────────────────────────────────────
//
// ① 延迟出现 + 滑上滑下：`/clear` 这种零点几秒就结束的活，横幅会闪一下。
//    所以可见性由 `Composer` 那边延迟 750ms 决定（`open`），这里只负责动画。
//    关键一条：计时不跟着延迟 —— `startedAt` 是这一轮真开始的时刻，
//    所以横幅一露头显示的就已经是 ~0.75s。
//    用时在 10 秒以内给两位小数 —— 否则第一眼看到的是「0.8s」而不是「0.75s」。
//
// ② `translateY(100%)` 是"藏在发送框后面"，不是"被父元素裁掉"。
//    `.status-slot` 高度 0↔38px、`.status-bar` 位移 100%↔0 同步跑 ——
//    算一下横幅顶边（`bottom` = 发送框顶边）：
//        top = bottom - 38h + 38(1-t)，展开到 h=t=1 时 = bottom - 38 ✓ 露出来
//                                          h=t=0.5   时 = bottom        （刚好露头）
//    所以后半程它从发送框上沿后面升起来，走的时候再沉回去。
//    因此 `.composer` 必须盖在它上面（`position:relative; z-index:1`）——
//    不然按 CSS 的绘制顺序，横幅里那些行内内容（图标/文字）会浮到发送框背景之上。

/** 计时。`waiting` = 正卡在等审批（审批期间暂停计时）。
 *
 *  判据：那段时间不是它在干活 —— 它在等人。算进去会把"审批等了 46 秒"
 *  记成"这一轮跑了 46 秒"，而那两件事的读数该分开看（同 `近似` 那族：
 *  同一组标签不许打两个量纲）。
 *  实现上不是"停止 tick"（那样恢复时数字会跳），而是累计暂停时长：
 *  暂停期间把显示冻在进入那一刻，恢复后从冻住的值继续往上走。
 */
import { t } from "./i18n.js";
function useElapsed(since, running, waiting) {
  const [, tick] = useState(0);
  const acc = useRef(0);        // 累计"被暂停"的毫秒
  const at = useRef(0);         // 本次暂停开始的时刻（0 = 没在暂停）
  useEffect(() => {
    if (!running || !since) return undefined;
    const timer = setInterval(() => tick((n) => n + 1), 100);
    return () => clearInterval(timer);
  }, [since, running]);
  //:换了一轮，两个账都清零（否则上一轮的暂停时长会被记到这一轮头上）。
  useEffect(() => { acc.current = 0; at.current = 0; }, [since]);
  useEffect(() => {
    if (waiting) { if (!at.current) at.current = Date.now(); return undefined; }
    if (at.current) { acc.current += Date.now() - at.current; at.current = 0; }
    return undefined;
  }, [waiting]);
  if (!since) return "";
  const stop = waiting && at.current ? at.current : Date.now();
  return fmtDuration((stop - since - acc.current) / 1000);
}

function useSeconds(since) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (!since) return undefined;
    const timer = setInterval(() => tick((n) => n + 1), 500);
    return () => clearInterval(timer);
  }, [since]);
  if (!since) return "";
  return Math.max(0, Math.round((Date.now() - since) / 1000)) + "s";
}

export function StatusBar({ phrase, startedAt, running, outputTokens, waiting,
                           retry, onCancel, onDismissError,
                           //:压缩中（黄横幅）/ 压缩失败（红横幅）。
                           //:   与 `retry` 同一条横幅 —— 判据是"发送框上方那一条"，
                           //:   不是再起一个提示条（用户一眼只该看一个地方）。
                           compacting, compactFailed, compactNothing, compactSince,
                           onDismissCompact,
                           compactDone, compactNotice, compactMessage }) {
  const elapsed = useElapsed(startedAt, running, waiting);
  //:压缩计时（横幅上那句「正在压缩，请稍后…  xxS」用它）。
  //:   单独一个 hook：它与 `useElapsed` 的暂停语义无关（压缩不会被"等待"打断）。
  const compactSecs = useSeconds(compactSince);

  //:压缩失败 —— 最高优先级：压过还是超，这一轮已经没救了。
  if (compactFailed) {
    return (
      <div className="status-slot open">
        <div className="status-bar retry compact-failed">
          <span className="status-bar-alert"><TriangleAlert size={14} /></span>
          <span className="status-bar-retry-text">
            <b>{t("API错误，消息容量超出API限制，需要重新启动")}</b>
          </span>
          {onDismissCompact ? (
            <button className="status-bar-cancel" onClick={onDismissCompact}
                    title={t("收起这条提示")}>
              <X size={12} />{t("知道了")}
            </button>
          ) : null}
        </div>
      </div>
    );
  }

  //:没得压—— `/compact` 回「没有压缩。可压的轮次不够…」时
  //:的横幅。它不是错误：这一轮一条请求都没发，也不存在超限。
  //:，黄横幅 + 一句话 + 「知道了」，点完就消失。
  //:以前这一支走的是 `fail`，于是界面翻成红横幅
  //:   「API错误，消息容量超出API限制，需要重新启动」—— 两件不相干的事。
  if (compactNothing) {
    return (
      <div className="status-slot open">
        <div className="status-bar notice">
          <span className="status-bar-alert"><TriangleAlert size={14} /></span>
          <span className="status-bar-text">
            <b>{t("当前上下文容量未达最低压缩量")}</b>
          </span>
          {onDismissCompact ? (
            <button className="status-bar-cancel" onClick={onDismissCompact}
                    title={t("收起这条提示")}>
              <X size={12} />{t("知道了")}
            </button>
          ) : null}
        </div>
      </div>
    );
  }

  //:压缩收尾的两种「说一句」：
  //:   `done`   —— 绿条：完成了，当前占用多少，停几秒自己收；
  //:   `notice` —— 黄条：降级/熔断（这次压得不完整），留着等用户点「知道了」。
  if (compactDone || compactNotice) {
    return (
      <div className="status-slot open">
        <div className={"status-bar " + (compactDone ? "compact-done" : "notice")}>
          {compactDone
            ? <span className="status-bar-alert"><Check size={14} /></span>
            : <span className="status-bar-alert"><TriangleAlert size={14} /></span>}
          <span className="status-bar-text">
            <b>{compactMessage || (compactDone
                                    ? t("压缩完成。")
                                    : t("上下文压缩失败——重启应用以继续此对话"))}</b>
          </span>
          {compactNotice && onDismissCompact ? (
            <button className="status-bar-cancel" onClick={onDismissCompact}
                    title={t("收起这条提示")}>
              <X size={12} />{t("知道了")}
            </button>
          ) : null}
        </div>
      </div>
    );
  }

  //:正在压缩（手动 `/compact` 与超长自救共用这一条）——
  //:   黄色而不是绿色：它是一件"系统正在动你的历史"的事，不是"模型在想"。
  if (compacting) {
    return (
      <div className="status-slot open">
        <div className="status-bar compacting">
          <span className="status-bar-spin"><Loader2 size={14} /></span>
          <span className="status-bar-text"><b>{t("正在压缩，请稍后…")}</b></span>
          <span className="status-bar-secs">{compactSecs}</span>
        </div>
      </div>
    );
  }

  //: 出错的横幅。
  //:   —— 「绿色横幅」就是这一条（发送框上方那条），不是对话流里的活动块。
  //:红条不滚（没有光带 / 没有转轮），字是纯白。
  //:
  //: 三个阶段（`retry.phase`）：`retry` 常规重试（带 n/max）· `silent`
  //:   静默期（每 120 秒一次，不报次数）· `failed` 收尾（这一轮已经结束，
  //:   横幅留着给用户看，右侧按钮从「打断」换成「知道了」）。
  if (retry) {
    const phase = String(retry.phase || "retry");
    const failed = phase === "failed";
    const showCount = !failed && phase !== "silent";
    return (
      <div className="status-slot open">
        <div className="status-bar retry">
          <span className="status-bar-alert"><TriangleAlert size={14} /></span>
          <span className="status-bar-retry-text">
            <b>{retryLabel(retry.kind, retry.text)}</b>
            {showCount ? (
              <span className="status-bar-retry-num">
                {retryCounter(retry.retry, retry.max)}
              </span>
            ) : null}
          </span>
          {failed && onDismissError ? (
            <button className="status-bar-cancel" onClick={onDismissError}
                    title={t("收起这条提示")}>
              <X size={12} />{t("知道了")}
            </button>
          ) : (onCancel ? (
            <button className="status-bar-cancel" onClick={onCancel}
                    title={t("打断这一轮（重试也会一起停）")}>
              <X size={12} />{t("打断")}
            </button>
          ) : null)}
        </div>
      </div>
    );
  }

  // 永远渲染（不 `return null`）—— 退出动画要元素留在 DOM 里才播得完。
  //   闲着的时候它是「高度 0 的槽 + 沉在发送框后面的条」，看不见也点不着。
  return (
    <div className={"status-slot" + (running ? " open" : "")}>
      <div className="status-bar">
        <span className="status-bar-spin"><Loader2 size={14} /></span>
        {/* 这句话与右边的用时/token 共用一条光带 ——
            所以渐变挂在这一个父元素上，靠 `background-clip:text` 一次盖住
            这两段全部的字形。给它俩各挂一份动画就会变成"各闪各的"。 */}
        <span className="status-bar-text">
          <b className="status-bar-phrase">{t(phrase)}…</b>
          <span className="status-bar-num">
            {elapsed}
            {outputTokens ? ` / ${fmtCount(outputTokens)} token` : ""}
          </span>
        </span>
      </div>
    </div>
  );
}
