import React, { useEffect, useRef, useState } from "react";

// 「接管模式」的安全警告。
//
// 三件事分开：
//   · 取消 = 不启用；
//   · 启用 = 真的启用（前 3 秒不可点，给用户读完那句话的时间）；
//   · 「我知道了」 = 只决定"以后还弹不弹这个窗口" —— 它不影响另外两个
//     按钮的含义（实现是一个 localStorage 偏好，同安全提示）。
//
// 那句正文不许"顺手润色" —— 它要说的正是
//   "接管期间不要干预输入，干预就退出"。
// 《接管模式说明》不存在，按占位渲染、不装成链接
//   （不许"点了没反应"）。
import { t } from "./i18n.js";
export function TakeoverNotice({ audioUrl, onConfirm, onCancel, onAcknowledge, onOpenDoc }) {
  const [left, setLeft] = useState(3);
  const audioRef = useRef(null);

  // 与 `SafetyNotice` 同一条：音频在挂载那一刻播（弹窗是点出来的 = 有用户手势）。
  useEffect(() => {
    if (!audioUrl) return undefined;
    let a = null;
    try {
      a = new Audio(audioUrl);
      a.volume = 0.7;
      audioRef.current = a;
      const p = a.play();
      if (p && typeof p.catch === "function") p.catch(() => {});
    } catch { /* 造不出来就当没有音频，不拦弹窗 */ }
    return () => { try { if (a) { a.pause(); a.currentTime = 0; } } catch { /* 忽略 */ } };
  }, [audioUrl]);

  useEffect(() => {
    if (left <= 0) return undefined;
    const timer = setTimeout(() => setLeft((n) => n - 1), 1000);
    return () => clearTimeout(timer);
  }, [left]);

  return (
    <div className="backdrop">
      <section className="safety" role="dialog" aria-modal="true" aria-labelledby="takeover-title">
        <div className="safety-head">
          <h2 className="safety-title" id="takeover-title">{t("安全警告")}</h2>
        </div>
        <p className="safety-body">
          {t("你正在启用Agent的")} <b>{t("接管模式")}</b>{t("，此模式下agent可以直接控制您的输入器以完成操作。")}
          <br />
          {/* 措辞的关键：检测不是从"启用"开始，而是从
              agent 真正开始操作那一刻开始 —— 启用之后用户仍要打字下指令、
              打开目标应用，那些不该被算成干预。写"接管期间"会变成一句假话。 */}
          {t("请注意，")}<b>{t("不要在 Agent 操作电脑期间进行干预输入")}</b>{t("，任何的干预输入都会直接退出接管。")}
          <br />
          {t("若要停用接管模式，请再次点击按钮。有关接管模式的详细内容，见")}
          {/* 说明页已存在（`HelpPanel.jsx` + `docs/help/takeover.md`），真按钮。 */}
          <button type="button" className="safety-link"
                  onClick={() => onOpenDoc && onOpenDoc("takeover")}>{t("《接管模式说明》")}</button>。
        </p>
        <div className="safety-actions">
          <button className="safety-go" disabled={left > 0} onClick={() => onConfirm()}>
            {left > 0 ? t("启用（{left} 秒）", { "left": left }) : t("启用")}
          </button>
          <button className="safety-no" onClick={onCancel}>{t("取消")}</button>
          <button className="safety-ack" onClick={onAcknowledge}>{t("我知道了")}</button>
        </div>
      </section>
    </div>
  );
}
