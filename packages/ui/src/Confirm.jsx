import React from "react";
import { t } from "./i18n.js";

/** 破坏性动作的二次确认。
 *
 *  判据：不可逆的动作一次点击不许发生 —— 解散群聊、删除对话这类
 *    点错就没了的东西，必须多一次确认。
 *  外壳照 `SafetyNotice` 那一套（`.backdrop` + `.safety`），不新造第三种浮窗。
 *  这里只负责问：调哪个接口、忙不忙由调用方管（`busy` 时两个按钮都禁用，
 *    防止连点发两次请求）。
 */
export function Confirm({ title, body, confirmLabel, cancelLabel, busy = false,
                          onConfirm, onCancel }) {
  return (
    <div className="backdrop" onClick={busy ? undefined : onCancel}>
      <section className="safety" role="dialog" aria-modal="true"
               onClick={(e) => e.stopPropagation()}>
        <div className="safety-head">
          <h2 className="safety-title">{title}</h2>
        </div>
        <p className="safety-body">{body}</p>
        <div className="safety-actions">
          <button className="safety-go" disabled={busy} onClick={onConfirm}>
            {confirmLabel}
          </button>
          <button className="skill-btn" disabled={busy} onClick={onCancel}>
            {cancelLabel || t("取消")}
          </button>
        </div>
      </section>
    </div>
  );
}
