// 「接管」按钮 / 警告弹窗的纯判据。
//
// 为什么抽成纯函数：这些都是"界面在说什么"的判据（标签不许说谎）：
// 按钮可不可点、禁用时那句提示是什么、退出时说哪句话。
// 放在 `.jsx` 里就只能靠人工走查；放这里，`packages/ui/tests` 能用 node 直接钉住。

import { t } from "./i18n.js";

export const TAKEOVER_NOTICE_KEY = "crannia.takeover-notice.off";

/** 用户点过「我知道了」没有（= 以后不再弹那个警告）。
 *  存储读写一律包 try：隐私模式下 `localStorage` 会抛，
 *  那时应当照常弹（fail-open 到"提醒"这一侧），而不是让界面挂掉。 */
export function takeoverNoticeOff() {
  try { return localStorage.getItem(TAKEOVER_NOTICE_KEY) === "1"; } catch { return false; }
}
export function rememberTakeoverNotice() {
  try { localStorage.setItem(TAKEOVER_NOTICE_KEY, "1"); } catch { /* 存不了就每次都问 */ }
}
/** 设置页「恢复安全提示」—— 下次重新弹这个警告。
 *  清的是提示偏好，不是接管状态；接管开着时清它不会改变任何行为。 */
export function clearTakeoverNotice() {
  try { localStorage.removeItem(TAKEOVER_NOTICE_KEY); } catch { /* 清不了就保持现状 */ }
}

/** 这个按钮现在能不能按 —— 不能时那句悬停提示是什么。
 *
 *  仅完全权限模式可用，其他模式下鼠标指针放上去要
 *     提示需要更高权限，并且点不动。禁用必须有理由，不许只灰掉。 */
export function takeoverAvailability({ mode, connected, enabled, desktop }) {
  if (enabled) return { ok: true, reason: "" };          // 开着时永远可以点（= 停用）
  // 只在桌面壳里可用 —— 浏览器里没有那条顶层横幅，而"我在动你的鼠标"
  //    必须整个接管期间一直可见。
  //    所以没有 `takeoverOverlay` 这扇门就没有这道保证，不许开。
  if (!desktop) {
    return { ok: false, reason: t("接管仅在桌面版可用。") };
  }
  if (!connected) return { ok: false, reason: t("服务未连接") };
  if (mode !== "full_access") {
    return { ok: false, reason: t("需要更高权限，请先把模式切到「完全权限」。") };
  }
  return { ok: true, reason: t("启用接管：Agent 可以直接控制你的鼠标键盘") };
}

/** 按钮上的字。 */
export function takeoverLabel(status) {
  return status && status.enabled ? t("接管中") : t("接管");
}

/** 退出接管时给用户的那句话（`last_stop.reason` 是后端填的机器码）。
 *  不认识的原因返回空串 —— 界面不编造理由。 */
export function stopReasonText(reason) {
  const r = String(reason || "");
  if (r === "esc") return t("已退出接管（Esc）");
  if (r === "physical_key") return t("已退出接管：检测到你的键盘输入");
  if (r === "physical_mouse") return t("已退出接管：检测到你的鼠标操作");
  if (r === "tray") return t("已退出接管（托盘）");
  if (r.startsWith("mode=")) return t("已退出接管：模式已切换");
  return "";
}
