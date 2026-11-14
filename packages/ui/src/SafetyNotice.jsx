import React, { useEffect, useRef, useState } from "react";

// 「手动切到自动模式」那一次的安全提示。
//
// 只有一条路会走到这里：用户在手动模式下主动切到自动
//    （`App.jsx::changeMode` 那三个条件）。切到手动 / 计划、或者重复点自动
//    —— 一次都不弹。
//
// 「不再提醒」存 `localStorage`，不进 `data/settings.json`：
//   它是这个壳的界面偏好（换个浏览器/清缓存就该重新问），
//   不是框架行为设置。读写一律包 `try` —— `localStorage` 在某些
//   上下文里会抛（隐私模式 / 禁用存储），那时应当照常弹（fail-open
//   到"提醒"这一侧），而不是让整个界面挂掉。
import { t } from "./i18n.js";
const DONT_REMIND_KEY = "crannia.auto-mode-notice.off";

export function autoNoticeOff() {
  try { return localStorage.getItem(DONT_REMIND_KEY) === "1"; } catch { return false; }
}
export function rememberAutoNotice() {
  try { localStorage.setItem(DONT_REMIND_KEY, "1"); } catch { /* 存不了就每次都问 */ }
}
/** 设置页那一格「恢复安全提示」—— 清掉偏好，下次重新弹。
 *  它只影响提示，不动任何权限/白名单；清不掉（隐私模式）也不算失败
 *  —— 那本来就是"每次都提醒"的状态（fail-open 到安全那一侧）。 */
export function clearAutoNotice() {
  try { localStorage.removeItem(DONT_REMIND_KEY); } catch { /* 清不了就保持现状 */ }
}

/** 安全提示弹窗。
 *
 *  音频在挂载那一刻就播。
 *    它能播出来是因为弹窗是点出来的 —— 那次点击就是一个用户手势，
 *    浏览器的自动播放策略放行。所以别把它改成"页面加载时自动弹"，
 *    那样音频会被静默拦掉（`.play()` 的 rejection 这里刻意吞掉不报：
 *    播放失败只是少一声响，不该在界面上再弹一条红字）。
 */
export function SafetyNotice({ audioUrl, onConfirm, onCancel, kind = "auto", onOpenDoc }) {
  const [remember, setRemember] = useState(false);
  const audioRef = useRef(null);

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

  // 点遮罩不关它 —— 这一屏要用户真的读一眼并做一次选择，
  //   不像别的弹窗"点外面 = 取消"（那会让误触直接等于"不切"，
  //   用户以为切了、其实没切）。
  return (
    <div className="backdrop">
      <section className="safety" role="dialog" aria-modal="true" aria-labelledby="safety-title">
        <div className="safety-head">
          <h2 className="safety-title" id="safety-title">{t("安全提示")}</h2>
        </div>
        <p className="safety-body">
          {kind === "full_access" ? (
            <>
              {t("你正在将框架运行模式切换至")} <b>{t("完全权限")}</b>{t("，此模式下 agent 将获得最大权限能力。 agent 的行动将不再询问您，仅有部分硬性边界生效。 请注意，您仍然是您的生产安全第一负责人。")}
            </>
          ) : (
            <>
              {t("你正在将框架运行模式切换至自动模式，此模式下 agent 的请求将移交自动审查。 绝大多数情况下，agent 的行动将不再询问您。请注意，您仍然是您的生产安全第一负责人。 有关自动模式的详细介绍，详见")}
              {/* 说明页已存在（`HelpPanel.jsx` + `docs/help/auto.md`）
                  这里是一颗真按钮：点开同一张卡片变大，左上角返回。 */}
              <button type="button" className="safety-link"
                      onClick={() => onOpenDoc && onOpenDoc("auto")}>{t("《自动模式说明》")}</button>。
            </>
          )}
        </p>
        <div className="safety-actions">
          <button className="safety-go" onClick={() => onConfirm(remember)}>{t("启用")}</button>
          <button className="safety-no" onClick={onCancel}>{t("取消")}</button>
          {/* 完全权限不给「不再提醒」 —— 它每次都要确认。
              持久化的「不问而放」必须每次经过一次人的动作。 */}
          {kind === "full_access" ? null : (
            <label className="safety-remember">
              <input type="checkbox" checked={remember}
                     onChange={(e) => setRemember(e.target.checked)} />
              {t("不再提醒")}
            </label>
          )}
        </div>
      </section>
    </div>
  );
}
