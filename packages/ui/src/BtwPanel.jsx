import React from "react";
import { Loader2, X } from "lucide-react";

// `/btw` 的浮窗 —— 题外话的回答只活在这里，关掉就没了。
//
// 判据：
//   · 与主对话分流（`POST /api/btw` 不进调度器）—— 主线程在跑时也能问，
//     不会排在那一轮后面；
//   · 不写 `messages`、不落库 —— 所以这里没有"历史"可回读；
//   · 关掉即丢弃：内容是瞬时的，重开不会再次出现。

import { t } from "./i18n.js";
export function BtwPanel({ data, close }) {
  if (!data) return null;
  return (
    <div className="btw-backdrop" onClick={close}>
      <section className="btw-panel" onClick={(e) => e.stopPropagation()}
               role="dialog" aria-live="polite">
        <header className="btw-head">
          <b>{t("顺带一问")}</b>
          <button className="round" onClick={close} title={t("关闭")}>
            <X size={14} />
          </button>
        </header>
        {/* 上面右边那句 = 用户问的话；下面 = 回答（先「响应中…」）。 */}
        <div className="btw-q">{data.question}</div>
        <div className="btw-a">
          {data.loading ? (
            <span className="btw-wait">
              <Loader2 size={14} className="btw-spin" />{t("响应中…")}
            </span>
          ) : (data.error || data.answer || t("没有拿到回复"))}
        </div>
      </section>
    </div>
  );
}
