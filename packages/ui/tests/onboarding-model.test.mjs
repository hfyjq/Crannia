// 新手引导 / 通道卡的纯函数读者 —— 零依赖，node 直接跑。
//
//   跑法： node packages/ui/tests/onboarding-model.test.mjs
//   （`tests/test_desktop_onboarding.py` 会跑它）
//
// 这里钉的是前端那一半的边界：步骤、校验、请求体、微信 phase 映射。
//   判据的权威在后端（`web/server.py` 的端点、`core/model_configs.py`），
//   所以上限一律由参数传进来 —— 这个文件里不许出现任何一个写死的上限。

import assert from "node:assert/strict";
import {
  ONBOARD_STEPS, SKIP_COOLDOWN_MS, WECHAT_QR_POLL_MS, WECHAT_STATUS_POLL_MS,
  defaultModelLabel, hasModelConfigs, modelBody, modelStepVerdict,
  nextStep, prevStep, skipButtonState, skipClick, stepIndex,
  wechatPhaseView, wechatPollDelay, wechatQrView,
  workspaceBody, workspaceVerdict,
} from "../src/onboarding-model.js";
import {
  CHANNEL_IDS, SELECTABLE_CHANNELS, channelById, channelFieldsValid, channelIconUrl,
  channelNotice, channelSaveBody, channelStatusOf, setDefaultBody, startBody,
} from "../src/channels-model.js";
import { blankConfig } from "../src/endpoint-form.js";

let failed = [];
function check(label, ok, note = "") {
  if (ok) {
    console.log(`  [ok]   ${label}`);
  } else {
    failed.push(label);
    console.log(`  [FAIL] ${label}${note ? "  -- " + note : ""}`);
  }
}

const LIMITS = { maxPerCard: 10, maxName: 40, maxModel: 120, maxUrl: 300,
                 windowMin: 1024, windowMax: 10000000 };

// ── 1. 步骤 ───────────────────────────────────────────────────────────
check("五步 id 逐字恒定",
      JSON.stringify(ONBOARD_STEPS) === JSON.stringify(["welcome", "workspace", "model", "platform", "done"]));
check("stepIndex('model') === 2", stepIndex("model") === 2);
check("stepIndex(未知) 回 0（不崩）", stepIndex("nope") === 0);
check("nextStep(4) 停在 4", nextStep(4) === 4);
check("prevStep(0) 停在 0", prevStep(0) === 0);
check("nextStep(1) === 2", nextStep(1) === 2);

// ── 2. 工作区 ─────────────────────────────────────────────────────────
check("空工作区被拒", workspaceVerdict("").ok === false);
check("纯空白也被拒", workspaceVerdict("   ").ok === false);
check("合法路径被 trim", workspaceVerdict("  D:\\work  ").value === "D:\\work");
check("workspaceBody 不合法回 null", workspaceBody("") === null);
check("workspaceBody 合法回 {path}",
      JSON.stringify(workspaceBody(" D:\\work ")) === JSON.stringify({ path: "D:\\work" }));

// ── 3. 模型步骤（复用端配置的判据）────────────────────────────────────
const blank = blankConfig("gpt");
check("空模型名 -> 不通过", modelStepVerdict(blank, LIMITS).ok === false);
const good = { ...blank, model: "gpt-4o" };
check("填了模型名 -> 通过", modelStepVerdict(good, LIMITS).ok === true);
check("不合法时 modelBody 回 null", modelBody(blank, LIMITS) === null);
const body = modelBody({ ...good, name: " 我的 GPT ", base_url: " https://x.test/v1 ", api: " sk-x " }, LIMITS);
check("modelBody 带上卡片 id", body && body.card === "gpt");
check("modelBody trim 了名称 / 地址 / api",
      body && body.name === "我的 GPT" && body.base_url === "https://x.test/v1" && body.api === "sk-x");
check("高级选项收着时 window = null（交给查表/实测）", body && body.window === null);
check("能力默认两格 auto",
      body && body.capabilities.vision === "auto" && body.capabilities.audio === "auto");

const cards = [
  { id: "gpt", configs: [] },
  { id: "deepseek", configs: [{ id: "c1", name: "主力", model: "v4", is_default: true }] },
];
check("hasModelConfigs: 空表 false", hasModelConfigs([{ id: "gpt", configs: [] }]) === false);
check("hasModelConfigs: 有条目 true", hasModelConfigs(cards) === true);
check("defaultModelLabel 取 is_default 那条", defaultModelLabel(cards) === "主力");
check("defaultModelLabel 没有默认时回空串", defaultModelLabel([{ id: "gpt", configs: [] }]) === "");

// ── 4. 「暂不配置」两段式 ─────────────────────────────────────────────
check("第一次点击只举手", JSON.stringify(skipClick(false)) === JSON.stringify({ armed: true, skipNow: false }));
check("第二次点击才真跳", JSON.stringify(skipClick(true)) === JSON.stringify({ armed: true, skipNow: true }));
check("未举手时按钮可用", skipButtonState(false, 0).disabled === false);
check("冷却中按钮禁用", skipButtonState(true, 1500).disabled === true);
check("冷却中文案带秒数", skipButtonState(true, 1500).text.includes("2"));
check("冷却完按钮可用且文案是确认",
      skipButtonState(true, 0).disabled === false && skipButtonState(true, 0).text.includes("确认"));
check("冷却 3 秒", SKIP_COOLDOWN_MS === 3000);

// ── 5. 通道表 ─────────────────────────────────────────────────────────
check("六个通道 id 与后端一致",
      JSON.stringify(CHANNEL_IDS) === JSON.stringify(
        ["wechat_channel", "feishu_channel", "dingtalk_channel", "telegram_channel",
         "qq_bot_channel", "cli_channel"]));
check("微信是扫码类型", channelById("wechat_channel").kind === "wechat");
check("CLI 没有字段", (channelById("cli_channel").fields || []).length === 0);
check("界面列出的通道去掉 CLI（五个）", SELECTABLE_CHANNELS.length === 5);
check("界面列出的通道里没有 cli_channel",
      SELECTABLE_CHANNELS.every((c) => c.kind !== "cli" && c.id !== "cli_channel"));
check("新飞书配置：两格都要", channelFieldsValid("feishu_channel", {}, false).ok === false);
check("新飞书配置：只填一格不够", channelFieldsValid("feishu_channel", { app_id: "x" }, false).ok === false);
check("已配置的飞书：填一格就够",
      channelFieldsValid("feishu_channel", { app_secret: "y" }, true).ok === true);
check("CLI 永远通过", channelFieldsValid("cli_channel", {}, false).ok === true);
check("保存体 trim 并丢掉空字段",
      JSON.stringify(channelSaveBody("feishu_channel", { app_id: " x ", app_secret: "" }).fields)
      === JSON.stringify({ app_id: "x" }));
check("微信保存体没有 fields",
      !("fields" in channelSaveBody("wechat_channel", {})));
check("微信保存体只带 section/channel_id",
      channelSaveBody("wechat_channel", {}).section === "channel"
      && channelSaveBody("wechat_channel", {}).channel_id === "wechat_channel");
check("startBody", JSON.stringify(startBody("telegram_channel")) === JSON.stringify({ channel_id: "telegram_channel" }));
check("setDefaultBody", JSON.stringify(setDefaultBody("cli_channel")) === JSON.stringify({ channel_id: "cli_channel" }));

const fakeClient = { asset: (p) => p };
check("图标走 client.asset", channelIconUrl(fakeClient, "wechat_channel") === "/static/img/channel-wechat.svg");
check("没有 client 时回空串（不拼裸路径）", channelIconUrl(null, "wechat_channel") === "");
check("CLI 没有图标", channelIconUrl(fakeClient, "cli_channel") === "");

check("运行中优先于已配置",
      channelStatusOf(channelById("feishu_channel"), { configured: true }, true).label === "运行中");
check("已配置", channelStatusOf(channelById("feishu_channel"), { configured: true }, false).label === "已配置");
check("未配置", channelStatusOf(channelById("feishu_channel"), { configured: false }, false).label === "未配置");
check("CLI 始终可用", channelStatusOf(channelById("cli_channel"), {}, false).label === "始终可用");

// ── 6. 微信 phase 映射 ────────────────────────────────────────────────
const STARTING = wechatPhaseView("starting");
check("starting 要轮询、间隔 1.5s",
      STARTING.poll === true && STARTING.delay === WECHAT_QR_POLL_MS);
const QR = wechatPhaseView("qr_ready");
check("qr_ready 要轮询、间隔 2s", QR.poll === true && QR.delay === WECHAT_STATUS_POLL_MS);
check("scanned 要轮询", wechatPhaseView("scanned").poll === true);
check("confirmed 是终止态", wechatPhaseView("confirmed").terminal === true);
check("expired 可重试", wechatPhaseView("expired").retry === true);
check("error 可重试", wechatPhaseView("error").retry === true);
check("already_configured 是终止态", wechatPhaseView("already_configured").terminal === true);
check("running 要轮询", wechatPhaseView("running").poll === true);
check("未知 phase 回 idle（不崩）", wechatPhaseView("wat").label === wechatPhaseView("idle").label);
check("终止态不再给轮询间隔", wechatPollDelay("confirmed") === null);
check("qr_ready 的间隔是 2s", wechatPollDelay("qr_ready") === 2000);

// ── 7. 二维码取值：只认 qr_data_uri ───────────────────────────────
const q1 = wechatQrView({ qr_data_uri: "data:image/png;base64,AAA",
                          img_url: "https://example.test/encode-me",
                          qrcode_key: "KEY1" });
check("有 qr_data_uri 时就用它", q1.image === "data:image/png;base64,AAA" && q1.hasImage === true);
check("同时有 img_url 也不会拿它当图片", q1.image !== "https://example.test/encode-me");
check("qrcode_key 作为文字兜底保留", q1.key === "KEY1");
const q2 = wechatQrView({ img_url: "https://example.test/encode-me", qrcode_key: "KEY2" });
check("没有 qr_data_uri 时 image 为空", q2.image === "" && q2.hasImage === false);
check("没有 qr_data_uri 时回 qrcode_key 文字", q2.key === "KEY2");
check("空载荷不崩", wechatQrView(null).hasImage === false);

// ── 8. 接入前的提示（今天只有飞书：群聊未完工） ───────────────────
const nFeishu = channelNotice("feishu_channel");
const NOTICE_BODY = "当前通道群聊模式暂未完善，请勿接入群聊使用，避免隐私泄露";
check("飞书有接入前提示（群聊未完善）", nFeishu && nFeishu.body === NOTICE_BODY,
      JSON.stringify(nFeishu));
check("提示带标题 / 继续 / 取消三个词",
      nFeishu && nFeishu.title && nFeishu.ok && nFeishu.cancel);
// 确认那颗的文案是「我已知晓」（不是「仍然接入」）
check("确认按钮是「我已知晓」", nFeishu.ok === "我已知晓", JSON.stringify(nFeishu));
// 钉钉那一句「尚未完成验证」已经摘掉，
//    换成与飞书同一句群聊隐私提示。
const nDing = channelNotice("dingtalk_channel");
check("钉钉也是同一条群聊提示（不是验证状态）",
      nDing && nDing.body === NOTICE_BODY, JSON.stringify(nDing));
const nTel = channelNotice("telegram_channel");
check("Telegram 也是同一条群聊提示（用户 2026-10-25 要求）",
      nTel && nTel.body === NOTICE_BODY, JSON.stringify(nTel));
// QQ 与那三条同一句（用户 2026-11-13 要求「飞书他们同款的群聊警告」）。
const nQq = channelNotice("qq_bot_channel");
check("QQ 也是同一条群聊提示",
      nQq && nQq.body === NOTICE_BODY, JSON.stringify(nQq));
check("四条通道共用同一个 notice kind（文案只有一处）",
      nFeishu.body === nDing.body && nDing.body === nTel.body
      && nQq && nTel.body === nQq.body,
      JSON.stringify([nFeishu.body, nDing.body, nTel && nTel.body, nQq && nQq.body]));
check("每个有提示的通道都带标题/继续/取消",
      [nFeishu, nDing, nTel, nQq].every((n) => n && n.title && n.ok && n.cancel));
// Telegram也带群聊提示（用户要求四条通道口径一致）
check("没有提示的通道不弹", channelNotice("wechat_channel") === null
      && channelNotice("cli_channel") === null);
check("认不出的 id 也不弹", channelNotice("nope_channel") === null);

// ── 8. 收尾 ───────────────────────────────────────────────────────────
if (failed.length) {
  console.log(`\n[FAIL] ${failed.length} 项`);
  process.exit(1);
}
console.log("\n[ok] all checks passed");
