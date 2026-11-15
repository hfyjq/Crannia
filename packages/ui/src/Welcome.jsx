import React, { useMemo } from "react";

// 无对话时的启动界面，与 WebUI 的 `chat-welcome` 保持一致
// （`web/templates/chat.html`），不是「三意图 + 快捷入口」那套。
//
// 逐字对齐的三样：① 按小时挑问候句（front + back 两段拼）② logo 淡入 → 问候 → 提示
// ③ 从十个技能词里随机挑四个当标签。
// WebUI 那半还支持 `localStorage['crannia-anim-disabled']`（关动画）——
//   桌面壳里没有接（改设置要走别的路）。

/** 逐字抄自 `chat.html:118-125`。改这里之前先看那边有没有一起改。 */
import { lang, t } from "./i18n.js";
const POOLS = [
  { min: 6, max: 9.5, fronts: ["早", "早安", "早上好", "一日之计在于晨"], backs: ["从哪里开始？", "想做点什么？", "今天有什么安排？"] },
  { min: 9.5, max: 12, fronts: ["上午好", "日安"], backs: ["今天我能帮你什么？", "有什么工作？", "想做点什么？", "今天从哪里开始？"] },
  { min: 12, max: 13.5, fronts: ["中午好", "午安"], backs: ["注意休息", "需要我做点什么？", "有什么工作？", "想做点什么？"] },
  { min: 13.5, max: 17.5, fronts: ["下午好"], backs: ["要做些什么？", "有什么需要帮忙的？", "有些什么工作？", "我能帮你做点什么？", "有什么计划？", "记得多活动多喝水"] },
  { min: 17.5, max: 24, fronts: ["晚上好", "晚好", "辛苦了"], backs: ["有什么安排？", "我可以帮你做点什么？", "注意休息", "想做些什么？"] },
  { min: 0, max: 6, fronts: ["夜深了"], backs: ["想做些什么？", "有什么我可以帮忙的？", "我可以帮你做点什么？", "早点休息"] },
];

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

export function Welcome({ running, connected, logo, shown }) {
  const joiner = lang() === "en" ? ", " : "，";
  const greeting = useMemo(() => {
    const h = new Date().getHours();
    const pool = POOLS.find((p) => h >= p.min && h < p.max) || POOLS[0];
    return t(pick(pool.fronts)) + joiner + t(pick(pool.backs));
  }, []);

  return (
    //:`shown` 那个 class 是开屏三条淡入的开关 —— 见 `App.jsx` 里的长注释：
    //:   桌面壳在 `ready-to-show` 之后还要等 500ms 才 `show()`，
    //:而页面早就渲染了，动画在窗口还没显示时就播完（看起来像直接出现）。
    //:不给这个 class，三条动画一条都不放（`animation` 全挂在 `.welcome.on` 上）。
    <section className={"welcome" + (shown ? " on" : "")}>
      <div className="welcome-inner">
        {/* logo 地址必须由 App 传进来（它从壳注入的 base 拼出来）。
            写死 `/static/img/logo.svg` 的话，打包后页面来自 `file://`，
            会解析成 `file:///static/...`（盘根目录），裂图。 */}
        <img className="welcome-logo" src={logo} alt="Crannia" />
        <div className="welcome-greeting">{greeting}</div>
        <div className="welcome-hint">
          {!connected ? t("要使用 Agent，请先启动框架。") : running ? "" : t("框架已连接，但 Agent 还没启动。")}
        </div>
      </div>
    </section>
  );
}
