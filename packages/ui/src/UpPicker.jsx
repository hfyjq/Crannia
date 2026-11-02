// 上拉选择器 —— 一个组件，两处用（发送框的模式 / 投入档，与设置页的
// `choice` 行共用同一套外观）。
//
// 搬出来、两边 import，不是再写一个像的（一份口径一个地方）。
//
// 开合状态不在它自己手里：
//   它收 `open` 与 `onToggle` 两个 prop。若每个 picker 各自 `useState`
//   所以点了"模式"再点"投入档"，两个都开着（点另一个按钮时，那个按钮在
//   `.popover-host` 里，上一个的"点外面就关" handler 判定为"里面"，不关）。
//   一个共享状态天然互斥 —— 不需要在两个地方各写一次互相关闭的逻辑
//   （那正是两份会漂的形状）。
//
// `fixed` 那一档是为了"在弹窗 / 滚动容器里不被裁掉"：
//   `.picker-menu` 是 `position:absolute` + 向上展开，那是为发送框
//   （贴在窗口底部）量身定的。而设置弹窗里那一列是 `overflow:auto`
//   所以绝对定位的子节点会被裁掉（症状：菜单只露一半，或者撑出一条横向滚动条）。
//   所以`fixed` 时量一下按钮在哪、把菜单 portal 到 `body` 上，位置写死。
//   两条配套的纪律，缺一条就是"点了没反应"：
//     ① 菜单不再是 `.popover-host` 的后代，「点外面关」那个 handler
//        必须同时接受 `.picker-menu`（否则按下的那一刻就把它关了，
//        `click` 永远落不到菜单里的按钮上）；
//     ② `z-index` 要高过 `.backdrop`（10）—— 见 `styles.css` 的 `.picker-menu.fixed`。
import React, { useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronUp } from "lucide-react";

export function UpPicker({ name, open, onToggle, label, value, options, onPick,
                          disabled, fixed, title }) {
  const btn = useRef(null);
  //:只有 `fixed` 那一档用得上 —— 它是量出来的几何，不是开合状态。
  const [pos, setPos] = useState(null);

  //:`fixed` 那一档不能一律向下展开 —— 在群聊那些卡片上会出屏
  //:   （下半截看不见），而且列表一长就没救。
  //:，优先向上（上方的空间不小于下方就向上），
  //:并给一个最多 5 行的高度上限（超出上下滚动）。
  useLayoutEffect(() => {
    if (!fixed || !open) { setPos(null); return; }
    const el = btn.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const gap = 6;
    //: 5 行 × ~30px + 菜单内边距 —— 最多显示 5 个，多的支持上下滚动。
    const want = 5 * 30 + 8;
    const above = Math.max(0, r.top - gap);
    const below = Math.max(0, window.innerHeight - r.bottom - gap);
    const upward = above >= below;          // 上方够/更宽裕，向上
    const maxHeight = Math.max(60, Math.min(want, upward ? above : below));
    const width = Math.max(110, Math.min(r.width, 260));
    const next = {
      left: Math.max(8, Math.min(r.left, window.innerWidth - width - 8)),
      minWidth: width,
      maxHeight,
    };
    if (upward) { next.bottom = window.innerHeight - r.top + gap; next.top = "auto"; }
    else { next.top = r.bottom + gap; next.bottom = "auto"; }
    setPos(next);
  }, [fixed, open]);

  const current = options.find((o) => o.key === value);
  const menu = (
    <div className={fixed ? "picker-menu fixed" : "picker-menu"}
         style={pos || undefined}>
      {options.map((o) => (
        <button
          key={o.key}
          className={o.key === value ? "on" : ""}
          onClick={() => { onPick(o.key); onToggle(false); }}
        >
          {o.label}
          {o.key === value ? <Check size={13} /> : null}
        </button>
      ))}
    </div>
  );

  return (
    <div className="popover-host picker">
      <button ref={btn} className="picker-btn" disabled={disabled}
              aria-expanded={open}
              title={title || undefined}
              onClick={() => onToggle(!open)}>
        {current ? current.label : label}
        <ChevronUp size={13} />
      </button>
      {open ? (
        fixed
          // 量到位之前不画（画了会在 0,0 闪一下）
          ? (pos ? createPortal(menu, document.body) : null)
          : menu
      ) : null}
    </div>
  );
}
