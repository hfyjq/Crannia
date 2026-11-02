// 通道卡的判据 —— 纯函数，node 直接断言。
//
//   为什么单独一个文件（照 `endpoint-form.js` / `group-form.js` 那条）：
//   引导页和端配置页共用同一份通道知识，两处各写一遍必然漂。
//
// 五个 id 的权威在后端（`web/server.py::_detect_channels`）。
//    这里只抄"界面怎么画"，并由 `tests/test_desktop_onboarding.py`
//    拿两边的 id 集合对账 —— 漏一个就红。
//
// 不要用后端返回的 `channels[].icon` —— 它是一段 HTML 字符串
//    （`_detect_channels` 里拼出来的 `<img …>`）。注进 React 要么走
//    `dangerouslySetInnerHTML`（等于给持久化 XSS 开门），要么解析它。
//    两样都不值当，图标表只在这一份，路径只从 `client.asset` 出。

import { t } from "./i18n.js";

/** 五个通道。`kind` 决定弹窗画什么：
 *  · `wechat` 扫码登录（`WechatLogin.jsx`）
 *  · `form`   字段表 → `POST /api/config`
 *  · `cli`    终端式对话，始终可用，没有可配的字段
 */
export const CHANNELS = [
  { id: "wechat_channel", label: "微信", icon: "channel-wechat.svg", kind: "wechat" },
  { id: "feishu_channel", label: "飞书", icon: "channel-feishu.png", kind: "form",
    // 能力还没完工的通道：接入前先弹一句（文案在 `channelNotice`）。
    //   飞书与钉钉同一句：群聊那半还没完善，接群聊有隐私风险。
    notice: "group_unfinished",
    fields: [
      { key: "app_id", label: "App ID" },
      { key: "app_secret", label: "App Secret", secret: true },
    ] },
  { id: "dingtalk_channel", label: "钉钉", icon: "channel-dingtalk.png", kind: "form",
    // 那条「尚未完成验证」已经摘掉（四条都验过了）；
    //   换成的这一条不是验证状态，是群聊的隐私风险 —— 与飞书同一句。
    notice: "group_unfinished",
    fields: [
      { key: "app_key", label: "App Key" },
      { key: "app_secret", label: "App Secret", secret: true },
    ] },
  { id: "telegram_channel", label: "Telegram", icon: "channel-telegram.svg", kind: "form",
    // 与飞书 / 钉钉同一句：群聊那半还没完善，接群聊有隐私风险。
    notice: "group_unfinished",
    fields: [
      { key: "bot_token", label: "Bot Token", secret: true },
    ] },
  { id: "cli_channel", label: "CLI 终端", icon: "", kind: "cli" },
];

export const CHANNEL_IDS = CHANNELS.map((c) => c.id);

/** 界面上列出来的通道 —— 今天 = 去掉 CLI。
 *
 *  CLI 不是"要配置的通道"（它始终可用、没有字段、扫码也无从谈起）；
 *    用户两次点名「CLI 按钮去掉」，引导和端配置页都不画它。
 *  表本身（`CHANNELS`）仍保留它 —— `_detect_channels` 与测试按五个 id
 *    对账；`cli_channel` 作为 `config.yaml` 的 default 仍然是合法的。
 */
export const SELECTABLE_CHANNELS = CHANNELS.filter((c) => c.kind !== "cli");

export function channelById(id) {
  return CHANNELS.find((c) => c.id === id) || null;
}

/** 通道图标地址 —— 必须走 `client.asset`（与厂商图标同一条规矩）。
 *
 *  回 `""` = 没有图标，用兜底字形（CLI 就是这一档）。
 *  不许写 `"/static/img/" + f` 这种兜底：打包后那是盘根目录，裂图
 *     （`tests/test_desktop_assets.py` 认的判据是"字面量必须直接是 `.asset(` 的实参"）。
 */
export function channelIconUrl(client, id) {
  const def = channelById(id);
  if (!def || !def.icon) return "";
  if (!client || typeof client.asset !== "function") return "";
  return client.asset("/static/img/" + def.icon);
}

/** 字段表该怎么校验。
 *
 *  两条，形状不同，理由也不同：
 *   · 新配置：后端只写收到的字段，缺一格就等于配了一半，全要。
 *   · 已配置：留空 = 不改那一格（后端的写入是"只写收到的"）
 *     至少填一格就够，允许用户只换 secret。
 */
export function channelFieldsValid(id, fields, configured) {
  const def = channelById(id);
  if (!def || def.kind !== "form") return { ok: true, error: "" };
  const f = fields || {};
  const keys = (def.fields || []).map((x) => x.key);
  const filled = keys.filter((k) => String(f[k] == null ? "" : f[k]).trim());
  if (configured) {
    return filled.length > 0
      ? { ok: true, error: "" }
      : { ok: false, error: t("至少填一格（留空表示不改那一格）") };
  }
  const missing = (def.fields || [])
    .filter((x) => !String(f[x.key] == null ? "" : f[x.key]).trim())
    .map((x) => x.label);
  return missing.length
    ? { ok: false,
        error: t("还要填：{fields}", { fields: missing.map((x) => t(x)).join(" / ") }) }
    : { ok: true, error: "" };
}

/** 保存通道配置的请求体。
 *
 *  空的字段不发 —— 后端 `_save_channel_config` 只写收到的键；
 *    发一个空串会把 `.env` 里原来的值清掉（而界面上看不出来）。
 *  微信 / CLI 没有字段：微信的凭据由扫码流程自己落盘，这里传一个
 *    `section/channel_id` 的 no-op（与旧 WebUI `finishWechatOnboard` 逐字同形）。
 */
export function channelSaveBody(id, fields) {
  const def = channelById(id);
  const body = { section: "channel", channel_id: id };
  if (!def || def.kind !== "form") return body;
  const f = fields || {};
  const out = {};
  for (const x of def.fields || []) {
    const v = String(f[x.key] == null ? "" : f[x.key]).trim();
    if (v) out[x.key] = v;
  }
  body.fields = out;
  return body;
}

export function startBody(id) {
  return { channel_id: id };
}

export function setDefaultBody(id) {
  return { channel_id: id };
}

/** 一张卡的状态行（`row` 来自 `GET /api/config.channels[]`，`running` 是布尔）。
 *
 *  判据的顺序是从"正在发生"到"静态"：运行中 > 已配置 > 未配置。
 *  一个正在跑的通道显示"未配置"会让用户以为它随时会掉。
 */
export function channelStatusOf(def, row, running) {
  // 状态行是每次渲染时算的（函数），在这里 `t()` 安全；表本身不动。
  if (def && def.kind === "cli") return { label: t("始终可用"), cls: "ch-status-idle" };
  if (running) return { label: t("运行中"), cls: "ch-status-ok" };
  if (row && row.configured) return { label: t("已配置"), cls: "ch-status-ok" };
  return { label: t("未配置"), cls: "ch-status-warn" };
}

/** 接入前必须看到的那句话（飞书 / 钉钉同一句）。返回 `null` = 不需要弹。
 *
 *  判据：能力没完工的通道，先弹一句再让配。
 *  配完之后才说"有些功能不好使"，用户会以为是我们坏了、或者他配错了。
 *  说的是群聊的隐私风险，不是"通道没验过" ——
 *  私聊那两半都验过了，剩下的风险在群聊（消息进来会被别人看到、也会把
 *  群里的内容送进模型）。两条通道共用一句，文案只有一处。
 *  文案写成 `t("…")` 字面量 —— 抽成常量的话扫描器扫不到，
 *  英文界面里会一直是中文（这个坑踩过一次）。
 */
export function channelNotice(id) {
  const def = channelById(id);
  if (!def || !def.notice) return null;
  if (def.notice === "group_unfinished") {
    return {
      title: t("接入前须知"),
      body: t("当前通道群聊模式暂未完善，请勿接入群聊使用，避免隐私泄露"),
      ok: t("我已知晓"),
      cancel: t("取消"),
    };
  }
  return null;
}
