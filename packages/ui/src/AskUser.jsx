import React, { useEffect, useRef, useState } from "react";
import {
  buildAnswer, canSubmit, hintText, keyAtDisplayNumber, toggleKey,
} from "./ask-user-model.js";

// 提问面板 —— 模型主动向用户出的选择题。
//
// 与审批浮窗是两种形状：
//    审批 = 求授权（有"同意 / 不同意"）；提问 = 问意见（选项是模型给的，
//    而且用户可以不选、直接说一句别的）。
//    所以不复用 `Approval.jsx`：把它们合成一个，就得在每个分支里问"这是哪种"，
//      而那正是"两套语义混在一处"的形状。
//
// 判据在 `ask-user-model.js`（纯函数，node 逐条断言）。本文件只做三件事：
//    把答案喂进去 · 画出来 · 按「确认」交给 `answer()`。

import { t } from "./i18n.js";
export function AskUser({ data, answer }) {
  const [keys, setKeys] = useState([]);
  const [text, setText] = useState("");
  const inputRef = useRef(null);
  const sectionRef = useRef(null);
  const rid = data && data.request_id;

  //:换了新的问题，选中与输入全归零（同一个组件会换载荷）。
  useEffect(() => {
    setKeys([]);
    setText("");
    // 与 `Approval` 同一个问题 —— 弹窗出现时焦点还在聊天框，
    //   数字键与回车都会被 `typing` 判据吞掉。把焦点搬进面板。
    //:同 `Approval`：同步 focus，不等下一帧（那一帧会被负载排到后面）。
    try { if (sectionRef.current) sectionRef.current.focus(); } catch { /* 忽略 */ }
  }, [rid]);

  function submit() {
    const r = buildAnswer(keys, text);
    if (!r.ok) return false;
    answer(r.body, data.request_id);
    return true;
  }

  useEffect(() => {
    function onKey(e) {
      const tag = (e.target && e.target.tagName) || "";
      const typing = tag === "INPUT" || tag === "TEXTAREA";
      if (typing && e.target !== inputRef.current) return;
      if (e.key === "Enter" && (typing || keys.length)) {
        if (submit()) e.preventDefault();
        return;
      }
      if (typing) return;
      if (/^[1-9]$/.test(e.key)) {
        const k = keyAtDisplayNumber(data, Number(e.key));
        if (k) {
          setKeys((cur) => toggleKey(data, cur, k));
          e.preventDefault();
        }
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [data, keys, text]);

  const opts = (data && data.options) || [];
  const isMulti = !!(data && data.multi);
  const on = canSubmit(keys, text);

  return (
    <div className="backdrop">
      <section className="approval ask-panel" ref={sectionRef} tabIndex={-1}>
        {/* 这里没有关闭 / 停靠 —— 与审批不同：提问是"这一轮张不开嘴"
            的唯一出路，收起来之后用户就没有地方回答它了（而审批还能去
            设置页 / 换个操作）。逃生口是下面那个输入框：不选、直接说。 */}
        <h2>{data.header || t("Crannia 想问你")}</h2>
        <p>{data.question || data.body || t("需要你定一下。")}</p>

        <div className="approval-options ask-options">
          {opts.map((o, i) => {
            const sel = keys.map(String).includes(String(o.key));
            return (
              <div className={"approval-option" + (sel ? " sel" : "")}
                   key={o.key}
                   onClick={() => setKeys((cur) => toggleKey(data, cur, o.key))}>
                <span className="approval-key">
                  {isMulti ? (sel ? "☑" : "☐") : `${i + 1}.`}
                </span>
                <span className="approval-label">
                  {o.label}
                  {o.description ? <small className="ask-desc">{o.description}</small> : null}
                </span>
              </div>
            );
          })}
        </div>

        <input className="approval-input" ref={inputRef} value={text}
               onChange={(e) => setText(e.target.value)}
               placeholder={t("或者：直接说你的想法（不用选）")} />

        <footer className="approval-foot">
          <span className="approval-hint">{t(hintText(data))}</span>
          <button className="approval-ok" disabled={!on} onClick={submit}>{t("确认")}</button>
        </footer>
      </section>
    </div>
  );
}
