import React, { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import {
  canSubmit, DOCK_LABEL, hintText, initialSel, keyAction, optionClass, submitBody,
} from "./approval-model.js";

// 审批浮窗 —— 从 `App.jsx` 拆出的独立导出组件。
//
// 判据全部在 `approval-model.js` 里（纯函数，node 能一条条断言，
//    见 `packages/ui/tests/approval-model.test.mjs`）。本文件只做三件事：
//    把事件喂进去 · 把返回值画出来 · 把"提交"交给 `answer()`。
//
// 拆成独立文件是为了能单独挂载并测量 DOM 行为（React 那一层需要
//    行为读者）。它必须是一个导出组件，不能藏在 `App.jsx` 里当私有函数。
//
// 交互约定：
//   · 单击不提交（两步确认）· 有选中态 · 键盘全套 · 升级项标出来
//   · `expect==='text'` 给输入框 · ✕ 是"停靠"（不是拒绝）
//   · 回传的 key 取自选项表 —— 不硬编码序号，否则对没有该项的选项表会传错

import { t } from "./i18n.js";
export function Approval({ data, answer }) {
  const [sel, setSel] = useState(() => initialSel(data));
  const [text, setText] = useState("");
  const [docked, setDocked] = useState(false);
  const inputRef = useRef(null);
  const sectionRef = useRef(null);
  const rid = data && data.request_id;

  //:换了新的待审批，三个态全归零。
  //:不能只 `useState(initialSel)` —— 同一个浮窗会换载荷，
  //:   不重置的话新载荷会带着上一次的选中项，而那一屏上可能压根没有选项行。
  //:停靠态也不继承 —— 上一轮收起来的地方，这一轮该重新弹出来。
  //:从停靠展开，把焦点搬回弹窗（否则回车又被聊天框吞掉）。
  //:首次挂载不走这里（挂载由下面 `[rid]` 那条负责）—— 避免抢输入框的 autoFocus。
  const dockSeenRef = useRef(false);
  useEffect(() => {
    if (!dockSeenRef.current) { dockSeenRef.current = true; return; }
    if (docked) return;
    requestAnimationFrame(() => {
      try {
        const el = (data && data.expect === "text") ? inputRef.current : sectionRef.current;
        if (el) el.focus();
      } catch { /* 忽略 */ }
    });
  }, [docked]);

  useEffect(() => {
    setSel(initialSel(data));
    setText("");
    setDocked(false);
    // 弹窗出现时焦点还在聊天框里，下面那条 `typing && !mine` 把
    //   Enter 整个吞掉了。把焦点搬进弹窗（`expect === "text"` 除外 ——
    //   那一屏要的是输入框自己的 autoFocus）。
    if (data && data.expect !== "text") {
      //:同步 focus（不再等 `requestAnimationFrame`）：弹窗一挂上就把焦点搬进来。
      //:   等下一帧的话，"节点已经在 DOM 里"与"焦点搬好了"之间有一个窗口 ——
      //:   有负载时那一帧可能排在键盘事件之后（回车被聊天框吞掉）。
      try { if (sectionRef.current) sectionRef.current.focus(); } catch { /* 忽略 */ }
    }
  }, [rid]);

  function submit() {
    const r = submitBody(data, sel, text);
    if (!r.ok) return false;
    answer(r.body, data.request_id);
    return true;
  }

  useEffect(() => {
    if (docked) return undefined;           // 收起来了就不抢键（见 `keyAction` 的）
    function onKey(e) {
      const tag = (e.target && e.target.tagName) || "";
      const typing = tag === "INPUT" || tag === "TEXTAREA";
      const mine = e.target === inputRef.current;
      //:别抢别人的键 —— 用户在发送框里打一个 `2`，不该被当成"选中第 2 项"。
      //:   而我们自己那个输入框要单独放行（回车确认，判据 ⑥ 的收尾动作）。
      if (typing && !mine) return;
      const r = keyAction(data, sel, e.key, { docked, typing });
      if (r.action === "none") return;
      if (r.action === "select" || r.action === "move") {
        if (r.sel) setSel(r.sel);
        e.preventDefault();
      } else if (r.action === "submit") {
        if (submit()) e.preventDefault();
      } else if (r.action === "dock") {
        setDocked(true);
        e.preventDefault();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [data, sel, text, docked]);

  //:停靠 = 收起来，不是拒绝。
  //:因此没有"直接拒绝"的快捷方式 —— 要拒绝就走选项表里那一项，
  //:才看得见自己拒绝的是什么。也不猜任何 key。
  if (docked) {
    return (
      <button className="approval-pill" onClick={() => setDocked(false)}
              title={t("点击展开（收起不会拒绝，审批仍然有效）")}>
        <ChevronUp size={13} />{t(DOCK_LABEL)}
      </button>
    );
  }

  const isText = data.expect === "text";
  const enabled = canSubmit(data, sel, text);

  return (
    <div className="backdrop">
      <section className="approval" ref={sectionRef} tabIndex={-1}>
        {/* 位置保留：右上角是用户找出口的本能落点，撤掉反而找不到；
            但语义是"收起来"，不是"拒绝"。 */}
        <button className="close" onClick={() => setDocked(true)}
                title={t("收起（不是拒绝）")}><ChevronDown size={17} /></button>
        <h2>{t("需要确认")}</h2>
        <p>{data.body || data.prompt || data.summary || t("Agent 请求执行一项操作。")}</p>

        {isText ? (
          <>
            <p className="approval-question">{data.question || t("请补充：")}</p>
            <input className="approval-input" ref={inputRef} autoFocus value={text}
                   onChange={(e) => setText(e.target.value)}
                   placeholder={t("说一句…")} />
          </>
        ) : (
          <div className="approval-options">
            {(data.options || []).map((o, i) => (
              // 画的是显示序号（连续），回传的仍是 `o.key`。
              //    键位是回传语义，不许为显示重排（见 `approval-model.js` 那段）；
              //    屏幕上不能出现跳号，两者分开算。
              <div className={optionClass(o, sel)} key={o.key}
                   onClick={() => setSel(o.key)}>
                <span className="approval-key">{i + 1}.</span>
                {/* 选项标签来自 `core/approval.py` 的 `Option` 表 ——
                    那是模块级常量（import 时翻会把语言定死），在这里翻。 */}
                <span className="approval-label">{t(o.label)}</span>
              </div>
            ))}
          </div>
        )}

        <footer className="approval-foot">
          <span className="approval-hint">{t(hintText(data))}</span>
          <button className="approval-ok" disabled={!enabled} onClick={submit}>{t("确认")}</button>
        </footer>
      </section>
    </div>
  );
}
