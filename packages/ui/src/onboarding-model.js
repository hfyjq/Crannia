// 新手引导的判据 —— 纯函数，node 直接断言。
//
//   为什么单独一个文件（照 `endpoint-form.js` / `group-form.js` 那条）：
//   步骤、校验、请求体、微信 phase 映射全是"边界"，而组件里的 `useState`
//   断不了边界。组件的 `src/Onboarding.jsx` 只管画。
//
// 模型那一步复用 `endpoint-form.js`（端配置页的同一份判据）——
//    两处各写一遍"什么算填好了"必然漂。上限也全由 `GET /api/model-configs`
//    传进来，这个文件里一个上限都不写死。
//
// 微信的 `img_url` 不是图片地址 —— 后端 `web/server.py` 的注释写着
//    它是"要编码进二维码的内容"（iLink 的 `qrcode_img_content`）。
//    真正的图是 `qr_data_uri`（后端用 qrcode 包生成的 PNG data URI）。
//    所以`wechatQrView` 只认 `qr_data_uri`，没有就回 `qrcode_key` 文字。

import { buildCreatePayload, validateConfig } from "./endpoint-form.js";

/** 五步（顺序就是界面上的顺序）。 */
import { t } from "./i18n.js";

export const ONBOARD_STEPS = ["welcome", "workspace", "model", "platform", "done"];

export function stepIndex(id) {
  const i = ONBOARD_STEPS.indexOf(id);
  return i < 0 ? 0 : i;
}

export function nextStep(i) {
  return Math.min(ONBOARD_STEPS.length - 1, Math.max(0, Number(i) + 1));
}

export function prevStep(i) {
  return Math.max(0, Math.min(ONBOARD_STEPS.length - 1, Number(i) - 1));
}

/** 「暂不配置」的二次确认冷却 —— 与旧 WebUI 逐字同量级（3 秒）。 */
export const SKIP_COOLDOWN_MS = 3000;

/** 微信扫码轮询的两档间隔（与旧 WebUI 用的值一致）。 */
export const WECHAT_QR_POLL_MS = 1500;
export const WECHAT_STATUS_POLL_MS = 2000;
/** 从"开始登录"到"拿到二维码"的最长等待。 */
export const WECHAT_QR_TIMEOUT_MS = 30000;

// ── 第 2 步：工作区 ───────────────────────────────────────────────────

export function workspaceVerdict(raw) {
  const v = String(raw == null ? "" : raw).trim();
  if (!v) return { ok: false, value: "", error: t("请选择或填写一个工作区目录") };
  return { ok: true, value: v, error: "" };
}

export function workspaceBody(path) {
  const v = workspaceVerdict(path);
  return v.ok ? { path: v.value } : null;
}

// ── 第 3 步：模型配置 ─────────────────────────────────────────────────

/** 校验（直接复用端配置那份判据；`limits` 由后端给）。 */
export function modelStepVerdict(form, limits) {
  return validateConfig(form, limits || {});
}

/** 建配置的请求体；不合法回 `null`（同 `buildCreatePayload`）。 */
export function modelBody(form, limits) {
  return buildCreatePayload(form, limits || {});
}

/** 卡片表里有没有任何一条已存配置（刷新后续跑时给「沿用现有配置」）。 */
export function hasModelConfigs(cards) {
  return (cards || []).some((c) => ((c && c.configs) || []).length > 0);
}

/** 默认那条的显示名（没有就给空串 —— 调用方自己决定怎么念）。 */
export function defaultModelLabel(cards) {
  for (const c of cards || []) {
    for (const row of (c && c.configs) || []) {
      if (row && row.is_default) return String(row.name || row.model || "");
    }
  }
  return "";
}

// ── 「暂不配置」的两段式 ───────────────────────────────────────────────

/** 点第一次 = 举手（进冷却）；冷却后再点 = 真跳。
 *  这一格没有第三种状态 —— "冷却中再点"由按钮的 `disabled` 挡掉，
 *    所以这里只回答"这一次点击算不算数"。 */
export function skipClick(armed) {
  return armed ? { armed: true, skipNow: true } : { armed: true, skipNow: false };
}

/** 按钮此刻长什么样。`remainingMs` 只在 `armed` 时有意义。 */
export function skipButtonState(armed, remainingMs) {
  if (!armed) return { disabled: false, text: t("暂不配置") };
  const ms = Number(remainingMs) || 0;
  if (ms > 0) return { disabled: true, text: t("跳过 ({s}s)", { s: Math.ceil(ms / 1000) }) };
  return { disabled: false, text: t("确认跳过 →") };
}

// ── 第 4 步：微信扫码 ─────────────────────────────────────────────────

/** 后端 `WebLoginState` 的 phase → 界面该怎么表现。
 *
 *  两档轮询间隔不同，理由也不同：
 *   · `starting`：还在等二维码，1.5s 问一次（旧 WebUI 用的值）
 *   · `qr_ready` / `scanned` / `running`：二维码已经在屏幕上，2s 问一次
 *     （扫完到确认之间是人在操作，问太快没有意义）
 */
const WECHAT_PHASES = {
  idle: { label: "等待开始", poll: false, terminal: false, retry: false },
  starting: { label: "正在唤起微信通道…", poll: true, delay: WECHAT_QR_POLL_MS,
    terminal: false, retry: false },
  qr_ready: { label: "请用微信扫描二维码", poll: true, delay: WECHAT_STATUS_POLL_MS,
    terminal: false, retry: false },
  scanned: { label: "已扫描，请在手机上确认登录…", poll: true, delay: WECHAT_STATUS_POLL_MS,
    terminal: false, retry: false },
  confirmed: { label: "登录成功", poll: false, terminal: true, retry: false },
  expired: { label: "二维码已过期", poll: false, terminal: true, retry: true },
  error: { label: "登录出错", poll: false, terminal: true, retry: true },
  already_configured: { label: "微信已连接", poll: false, terminal: true, retry: false },
  running: { label: "微信通道正在运行", poll: true, delay: WECHAT_STATUS_POLL_MS,
    terminal: false, retry: false },
};

export function wechatPhaseView(phase) {
  // 表是模块级常量，只翻 `label`（调用方拿到的就是当前语言）。
  const v = WECHAT_PHASES[String(phase || "")] || WECHAT_PHASES.idle;
  return { ...v, label: t(v.label) };
}

/** 下一次轮询等多久；`null` = 不要再轮询了。 */
export function wechatPollDelay(phase) {
  const v = wechatPhaseView(phase);
  return v.poll ? v.delay : null;
}

/** 二维码那块该画什么。
 *
 *  `img_url` 永远不进这张表 —— 它是"要编码的内容"，不是图片地址。
 *     拿它当 `<img src>` 的话，正常的二维码会变成一张裂图（旧 WebUI 就有
 *     这个分支，新界面不跟）。
 */
export function wechatQrView(payload) {
  const p = payload || {};
  const image = String(p.qr_data_uri || "");
  const key = String(p.qrcode_key || "");
  return { image, key, hasImage: !!image };
}
