// 「这次更新了什么」—— 装到新版本之后的第一次启动弹一次。
//
// 内容来自 `docs/updates/<id>.txt`（发版时手写，形状照仓库根那份
// `更新公告.txt`）。这里只负责画：标题一行、小节若干、条目按编号列。
//
// 「看过哪个 id」存 `localStorage` —— 与「自动模式提示」同一条判据：
//   它是这个壳的界面偏好，不是框架行为设置（换台机器 / 清了缓存就该再弹一次，
//   而那不是丢失，是"这个人还没看过这一份"）。
//   读写一律包 try：隐私模式里 `localStorage` 会抛，那时照常弹（fail-open
//   到"给用户看"这一侧），而不是让启动路径挂掉。
import { t } from "./i18n.js";

const SEEN_KEY = "crannia.update-notice.seen";

export function seenNoticeId() {
  try { return localStorage.getItem(SEEN_KEY) || ""; } catch { return ""; }
}

export function rememberNoticeId(id) {
  try { localStorage.setItem(SEEN_KEY, String(id || "")); } catch { /* 存不了就下次再弹 */ }
}

/** 设置页「恢复提示」那一格也清它（与 `clearAutoNotice` 同一条路）。 */
export function clearNoticeSeen() {
  try { localStorage.removeItem(SEEN_KEY); } catch { /* 清不了就保持现状 */ }
}

export function UpdateNotice({ notice, onClose }) {
  //: 刻意不抢焦点（安全提示那两颗也没有）：它是一次告知，不是一次确认；
  //:   开机时若同时挂着审批浮窗，抢焦点会让回车落到这里、把审批框晾着。
  if (!notice) return null;
  return (
    <div className="backdrop">
      <section className="update-notice" role="dialog" aria-modal="true"
               aria-labelledby="update-notice-title"
               onKeyDown={(e) => { if (e.key === "Escape") onClose(); }}>
        <h2 id="update-notice-title" className="update-notice-title">
          {notice.title || t("更新内容")}
        </h2>
        <div className="update-notice-body">
          {notice.sections.map((sec, i) => (
            <section key={(sec.head || "") + i} className="update-notice-sec">
              {sec.head ? <h3>{sec.head}</h3> : null}
              <ol>
                {sec.items.map((it, j) => <li key={j}>{it}</li>)}
              </ol>
            </section>
          ))}
        </div>
        <button type="button" className="update-notice-ok" onClick={onClose}>
          {t("我已知晓")}
        </button>
      </section>
    </div>
  );
}
