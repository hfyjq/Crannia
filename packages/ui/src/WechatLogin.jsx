import React, { useEffect, useRef, useState } from "react";
import { jsonRequest } from "./api.js";
import {
  WECHAT_QR_POLL_MS, WECHAT_QR_TIMEOUT_MS, WECHAT_STATUS_POLL_MS,
  wechatPhaseView, wechatPollDelay, wechatQrView,
} from "./onboarding-model.js";

// 微信扫码登录 —— 引导页与端配置页共用这一台。
//
// 判据全在 `onboarding-model.js`（纯函数，node 逐条断言）—— 这里只管画。
//
// 只渲染 `qr_data_uri`：后端把它生成为一张 PNG 的 data URI。
//    `img_url` 是 iLink 返回的 "要编码进二维码的内容"，不是图片地址
//    （`web/server.py::api_wechat_start_login` 的注释逐字写着这件事）。
//    旧 WebUI 有一支拿 `img_url` 当 `<img src>` —— 新界面不跟：
//    那会把一张正常的二维码画成裂图。
//
// 绝不调 `/api/restart`：它会重置所有通道，而微信的轮询循环
//    挂在 `channel.run()` 自己的 `self._running` 上，重启既停不掉旧线程，
//    又可能再起一个，两个线程用同一个 bot 抢消息。
//
// 覆盖重连：凭据已经存在 / 通道正在跑时，点「重新扫码登录」会带
//    `{"force": true}` 再打一次 `start-login` —— 后端先把旧通道停掉
//    （投递 `terminate()` 到它自己的事件循环），停稳之后再删旧凭据、
//    重新走扫码。旧线程没退出时后端回 500 + 路线，界面照实显示，
//    不会开第二个线程抢同一个 bot。

import { t } from "./i18n.js";
const BUSY_PHASES = ["starting", "running", "qr_ready", "scanned"];

export function WechatLogin({ client, connected, configured = false, onConfirmed, onRestart, onStart }) {
  const [view, setView] = useState({
    phase: configured ? "already_configured" : "idle",
    payload: null, botId: "", error: "",
  });
  const timerRef = useRef(null);
  const aliveRef = useRef(true);
  const startingRef = useRef(false);
  const startedAtRef = useRef(0);
  const failuresRef = useRef(0);
  const confirmedRef = useRef(false);

  function clearTimer() {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }

  function schedule(ms) {
    clearTimer();
    if (!aliveRef.current) return;
    timerRef.current = setTimeout(poll, ms);
  }

  function markConfirmed(botId) {
    if (confirmedRef.current) return;
    confirmedRef.current = true;
    if (typeof onConfirmed === "function") {
      try { onConfirmed(botId || ""); } catch (e) { /* 回调不许弄挂扫码 */ }
    }
  }

  async function poll() {
    timerRef.current = null;
    if (!aliveRef.current || !client) return;
    try {
      const r = await client.call("/api/wechat/qrcode");
      if (!aliveRef.current) return;
      failuresRef.current = 0;
      const phase = String((r && r.phase) || "idle");
      setView((v) => ({
        phase,
        payload: r || null,
        botId: (r && r.bot_id) || v.botId || "",
        error: (r && r.error_msg) || "",
      }));
      if (phase === "confirmed") { markConfirmed(r && r.bot_id); return; }
      if (phase === "expired" || phase === "error") return;
      if (phase === "idle" || phase === "starting") {
        if (Date.now() - startedAtRef.current > WECHAT_QR_TIMEOUT_MS) {
          setView((v) => ({ ...v, phase: "error", error: t("二维码获取超时，请重试。") }));
          return;
        }
        schedule(WECHAT_QR_POLL_MS);
        return;
      }
      const delay = wechatPollDelay(phase);
      if (delay) schedule(delay);
    } catch (e) {
      if (!aliveRef.current) return;
      failuresRef.current += 1;
      if (failuresRef.current >= 5) {
        setView((v) => ({ ...v, phase: "error", error: t("本地服务未连接，扫码流程已暂停。") }));
        return;
      }
      schedule(WECHAT_STATUS_POLL_MS);
    }
  }

  async function start(force = false) {
    if (!client || !connected) return;
    if (startingRef.current) return;
    if (BUSY_PHASES.includes(view.phase)) return;
    startingRef.current = true;
    clearTimer();
    failuresRef.current = 0;
    startedAtRef.current = Date.now();
    if (force && typeof onRestart === "function") {
      try { onRestart(); } catch (e) { /* 回调不许弄挂扫码 */ }
    }
    setView({ phase: "starting", payload: null, botId: "", error: "" });
    //:把上一次那句「微信登录成功」清掉 —— 否则重扫时它挂在下面，
    //:   看起来像"还没唤起就已经成功了"（用户现场）。
    if (typeof onStart === "function") {
      try { onStart(); } catch (e) { /* 回调不许弄挂扫码 */ }
    }
    try {
      const r = await client.call("/api/wechat/start-login",
                                  jsonRequest("POST", { force: !!force }));
      if (!aliveRef.current) return;
      const phase = String((r && r.phase) || "starting");
      if (phase === "already_configured") {
        setView({ phase, payload: r || null, botId: (r && r.bot_id) || "", error: "" });
        markConfirmed((r && r.bot_id) || "");
        return;
      }
      if (phase === "running") {
        setView({ phase, payload: r || null, botId: (r && r.bot_id) || "", error: "" });
        schedule(WECHAT_STATUS_POLL_MS);
        return;
      }
      setView({ phase: "starting", payload: r || null, botId: "", error: "" });
      schedule(WECHAT_QR_POLL_MS);
    } catch (e) {
      if (!aliveRef.current) return;
      setView({ phase: "error", payload: null, botId: "", error: e.message || t("启动失败") });
    } finally {
      startingRef.current = false;
    }
  }

  // 已配置时只读一次共享状态（不调 start-login —— 那会再起一个线程）。
  useEffect(() => {
    if (!configured || !client || !connected) return;
    //:凭据已经在盘上 = 已经连过了，立刻认「已连接」，
    //:否则引导页那个「从这里开始」会一直灰着（界面上又没有开始按钮，
    //:   因为 `canStart` 故意不给已配置的通道再起一个线程）。
    markConfirmed("");
    let alive = true;
    (async () => {
      try {
        const r = await client.call("/api/wechat/qrcode");
        if (!alive || !aliveRef.current) return;
        const phase = String((r && r.phase) || "");
        if (phase === "confirmed" || phase === "qr_ready" || phase === "scanned"
            || phase === "starting" || phase === "running") {
          setView((v) => ({ ...v, phase, payload: r || null, botId: (r && r.bot_id) || v.botId }));
          if (phase === "confirmed") markConfirmed(r && r.bot_id);
        }
      } catch (e) { /* 读不到就保持"已保存凭据"那一屏 */ }
    })();
    return () => { alive = false; };
  }, [configured, client, connected]);   // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => {
    aliveRef.current = false;
    clearTimer();
  }, []);

  const ph = wechatPhaseView(view.phase);
  const qr = wechatQrView(view.payload);
  const busy = BUSY_PHASES.includes(view.phase);
  //:已连接 / 正在跑也给这颗按钮 —— 它就是「覆盖重连」的入口。
  const already = view.phase === "confirmed" || view.phase === "already_configured"
    || view.phase === "running";
  const canStart = !!client && connected && !busy;
  const startLabel = already ? t("重新扫码登录")
    : (view.phase === "error" || view.phase === "expired") ? t("重试") : t("开始扫码登录");

  return (
    <div className="wx-login">
      {/* 「已连接」只说一次 —— 否则同一屏里「微信已连接」会出现两遍：
          这一行是 `already_configured` 的标签，下面那条绿的是带 Bot 号的那句。
          下面那条信息更全，已连接 / 在跑时这一行不渲染。 */}
      {!already && <p className="wx-status">{ph.label}</p>}

      {view.phase === "qr_ready" && qr.hasImage && (
        <div className="wx-qr">
          {/* 只认 qr_data_uri —— 见文件头。 */}
          <img className="wx-qr-img" src={qr.image} alt={t("微信登录二维码")} />
        </div>
      )}
      {view.phase === "qr_ready" && !qr.hasImage && (
        <div className="wx-qr">
          <code className="wx-qr-key">{qr.key || t("（没有拿到二维码内容）")}</code>
          <p className="wx-note">{t("二维码图片生成失败；可以复制上面的内容手动生成。")}</p>
        </div>
      )}

      {(view.phase === "qr_ready" || view.phase === "scanned" || view.phase === "starting") && (
        <ol className="wx-note">
          <li>{t("打开微信，进入扫一扫。")}</li>
          <li>{t("扫描上方二维码。")}</li>
          <li>{t("在手机上确认登录。")}</li>
        </ol>
      )}

      {(view.phase === "confirmed" || view.phase === "already_configured" || view.phase === "running") && (
        <p className="wx-connected">
          {t("微信已连接")}{view.botId ? <> · <span className="wx-bot">Bot {view.botId}</span></> : null}
        </p>
      )}

      {(view.phase === "error" || view.phase === "expired") && (
        <p className="wx-error">{view.error || ph.label}</p>
      )}

      {!connected && (
        <p className="wx-note">{t("本地服务未连接 —— 连上之后才能扫码。")}</p>
      )}

      <div className="wx-actions">
        {canStart && (
          <button className="skill-btn" disabled={!connected}
                  onClick={() => start(already)}
                  title={connected ? "" : t("本地服务未连接")}>
            {startLabel}
          </button>
        )}
        {busy && <span className="wx-note">{t("请稍候…")}</span>}
      </div>
    </div>
  );
}
