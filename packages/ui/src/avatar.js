// 群成员头像 —— 纯函数 + 一张表。
//
//   纯色几何图形是兜底形态；真头像资源由 `Avatar.jsx` 按同一槽位加载。
//
// 判据：
//   · 纯色几何图形不是图片、不引任何资源文件（零加载、零 404）；
//   · 槽位号（`0..7`）是键 —— 后端只存它（`core/groups.py::AVATAR_SLOTS`），
//     真头像按同一个槽位换，接口不用动；
//   · 表在这里、只有一份 —— 别在组件里再写一遍形状（两份会漂）。
//
// 这个模块不许 import React —— 它是给 node 直接断言的纯函数
//   （本包没有 vitest/jest，判据全靠 `packages/ui/tests/avatar.test.mjs`）。

/** 槽位个数。必须与 `core/groups.py::AVATAR_SLOTS` 相等。
 *
 *  那一边是权威（它写在磁盘的账本里，而且超界的槽位在入参那里就被拒了）
 *  —— 两边不一致的症状是"后端存了个 7、前端画成第 0 个"，看着像随机，其实是指错了。
 *  机械读者：`tests/test_desktop_avatar.py`（两边对账，少一边就红）。
 */
export const AVATAR_SLOTS = 8;

/** 八个图形 —— 每个就是一条 SVG 元素 + 属性（不是字符串拼出来的 HTML）。
 *
 *  用一个描述表而不是 `dangerouslySetInnerHTML`：那张表是数据，
 *    组件拿它 `createElement` —— 注入面为零（对 `innerHTML` 零容忍）。
 *  坐标系是 24×24（与 `Avatar.jsx` 的 `viewBox` 一致），改一个要一起改。
 */
export const SHAPES = [
  { name: "circle", el: "circle", attrs: { cx: 12, cy: 12, r: 10 } },
  { name: "square", el: "rect", attrs: { x: 3, y: 3, width: 18, height: 18, rx: 5 } },
  { name: "triangle", el: "polygon", attrs: { points: "12,2 22,21 2,21" } },
  { name: "diamond", el: "polygon", attrs: { points: "12,2 22,12 12,22 2,12" } },
  { name: "pentagon", el: "polygon", attrs: { points: "12,2 22,9.5 18.2,21 5.8,21 2,9.5" } },
  { name: "hexagon", el: "polygon", attrs: { points: "7.5,3 16.5,3 21,12 16.5,21 7.5,21 3,12" } },
  { name: "star", el: "polygon",
    attrs: { points: "12,2 15,9 22.5,9 16.5,13.6 18.6,21 12,17 5.4,21 7.5,13.6 1.5,9 9,9" } },
  { name: "semicircle", el: "path", attrs: { d: "M2 19 A10 10 0 0 1 22 19 Z" } },
];

/** 八个纯色 —— 与 `SHAPES` 同序：槽位 i 拿到第 i 个形状 + 第 i 个颜色。
 *
 *  同序是判据的一部分：错开取（`colors[i * 3 % 8]`）看着更"随机"，
 *  但那样 8 个槽位里会有颜色重复（8 与 8 互质才不重）—— 而"8 个人长得一模一样"
 *  正是这个功能要避免的事。
 */
export const COLORS = [
  "#4a8a52", "#3f7fbf", "#c2872f", "#a4547a",
  "#5a6f9c", "#b25a4a", "#4f8a8b", "#7a6bb0",
];

/** 槽位 → `{ shape, color, slot }`。永不抛（后端将来多给一个槽位也不该白屏）。
 *
 *  归一化只做两件事：非整数 → 0、超界 → 取模绕回来。
 *  不在这里"顺便"校验后端 —— 校验在 `core/groups.normalize_members`
 *    （入参那一刻就拒了）。这一层管的是画得出来。
 */
export function avatarFor(slot) {
  const n = AVATAR_SLOTS;
  const i = Number.isInteger(slot) ? ((slot % n) + n) % n : 0;
  return { slot: i, shape: SHAPES[i], color: COLORS[i] };
}

// 不要在图形上叠名字首字 —— 叠一个字在三角/星形上会从图形里戳出来。
//   八个槽位形状两两不同、颜色两两不同，本来就分得开（机械读者钉着这一条）；
//   多那一笔只是噪声。要用首字，先解决"它压在什么上"这件事。
