import React, { useEffect } from "react";
import { History, ListTree, Star, X } from "lucide-react";
import { HistoryPanel } from "./HistoryPanel.jsx";
import { StarsPanel } from "./StarsPanel.jsx";
import { TracePanel } from "./TracePanel.jsx";

// 右侧竖排栏 + 它的抽屉。
//
// 形态是"右侧浮出"（抽屉），不动 `.shell` 的三栏结构。
//
// 两个入口：
//     · ⏱ 历史记录 = `HistoryPanel`（地基是三条只读端点）
//     · 收藏夹   = `StarsPanel`（地基是 `stars` 表 + 三条端点）
//   不放放大镜。

import { t } from "./i18n.js";
const PANELS = [
  { key: "stars", label: "收藏夹", Icon: Star },
  { key: "history", label: "历史记录", Icon: History },
  //:任务清单这一格暂时不启用（`TodosPanel.jsx` 的实现留着，将来想启用时
  //:   把这一行与 `Drawer` 那一支加回来即可）。GUI 侧的前端改动先长在 Electron 里，
  //:   WebUI 那边另有一批。
  //:逐条轨迹 —— 参数全文 / 思考 / 结果实质。
  //:只在开关打开时才出现（打开设置后才在侧边栏显示此按钮）。
  { key: "trace", label: "轨迹", Icon: ListTree, needsTrace: true },
];

/** 右缘那条竖排栏（`.shell` 的第三列）。
 *
 *  `traceEnabled`：轨迹那一格只在开关打开时出现
 *  （打开设置后才在侧边栏显示此按钮）。值来自 `/api/status`
 *  （每 5 秒轮询，改完设置不用重启界面）。
 */
export function Rail({ open, onToggle, traceEnabled = false }) {
  const panels = PANELS.filter((p) => !p.needsTrace || traceEnabled);
  return (
    <aside className="rail">
      {panels.map(({ key, label, Icon }) => (
        <button
          key={key}
          className={"rail-btn" + (open === key ? " on" : "")}
          title={t(label)}
          aria-label={t(label)}
          aria-pressed={open === key}
          onClick={() => onToggle(open === key ? "" : key)}
        >
          <Icon size={18} />
        </button>
      ))}
    </aside>
  );
}

/** 从那一条右侧浮出来的面板。
 *
 *  永远渲染（收起时靠 `transform` 推到屏幕外）—— 这样进出场都有动画，
 *    不需要"再等动画播完才卸载"的那套状态机。
 *  它不是 `.shell` 的第四列，而是 `position:fixed` 的浮层：
 *    抽屉是"盖在对话上"的，不该把对话区挤窄。所以宽度不进 grid
 *    （`fixed` 元素本来也不占 grid 单元）。
 */
export function Drawer({ panel, onClose, client, jump, onJumped, onOpenStar }) {
  const cur = PANELS.find((p) => p.key === panel);

  // 点面板外面就关上（点非面板的页面区即关闭）。
  //
  // 点在右缘那排按钮上不算"外面" —— 那个按钮自己会 toggle；
  //   两边都响应的话，同一个按钮就再也关不上（mousedown 先关、click 又开）。
  //   同理 `.drawer` 里面也不算 —— 否则点面板自己就把它关了。
  // 所以判据是"既不在抽屉里、也不在右缘栏里"，其余一律关。
  useEffect(() => {
    if (!panel) return undefined;
    const away = (e) => {
      const node = e.target;
      if (!node || !node.closest) return;
      if (node.closest(".drawer") || node.closest(".rail")) return;
      onClose();
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [panel, onClose]);
  return (
    <div className={"drawer" + (panel ? " open" : "")}
         aria-hidden={panel ? "false" : "true"}>
      {cur ? (
        <>
          <div className="drawer-head">
            <b>{t(cur.label)}</b>
            <button className="round" onClick={onClose} title={t("关上")}><X size={16} /></button>
          </div>
          {/* 两个面板自己都是一整块（里面有滚动区），这一格要去掉内边距，
                 否则会套出两个滚动条。`.flush` 就是干这个的。 */}
          <div className={"drawer-body" + (panel === "history" || panel === "stars"
                                            || panel === "trace" ? " flush" : "")}>
            {panel === "history"
              ? <HistoryPanel client={client} jump={jump} onJumped={onJumped} />
              : panel === "stars"
                ? <StarsPanel client={client} onOpen={onOpenStar} />
                : panel === "trace"
                  ? <TracePanel client={client} />
                  // 这一支是未知 key 的兜底（当前 `PANELS` 里的每一格
                  //    都有对应面板，正常走不到）。
                  //    所以兜底话术只描述事实，不要提排期或计划 ——
                  //    留着就是一句会说谎的文案（界面在说谎那一族）。
                  : <p className="drawer-stub">{t("这一格没有面板。")}</p>}
          </div>
        </>
      ) : null}
    </div>
  );
}
