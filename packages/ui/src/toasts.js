// 右上角通知的队列规则（纯函数，node 可逐条断言）。
//
// 为什么单独一个模块：旧实现是单槽，`setToast(同一条文本)` 是 no-op
//    所以那个 2.8 秒的定时器不重置，第二条明明发生了、提示却按第一条的时钟
//    提前消失。那是行为 bug，不是观感问题，它得有读者（见
//    `tests/test_desktop_toasts.py`，真跑 node）。
//
// 三条规则：
//   ① 同样的文本再来一次 = 刷新它的时钟并挪到最下面（不重复堆同一句）；
//   ② 最多 `TOAST_MAX` 条，超了丢最旧的（栈太高会盖住正文）；
//   ③ 每条自己计时（`at`），到 `TOAST_TTL_MS` 由调用方扫掉。

export const TOAST_TTL_MS = 2800;

//: 三行以内还算"通知"；再多就变成一堵墙。
export const TOAST_MAX = 3;

/** 往栈里放一条。返回新数组（不原地改）。 */
export function pushToast(list, text, now = Date.now()) {
  const body = typeof text === "string"
    ? text
    : String(text === null || text === undefined ? "" : text);
  //: 空串不占一格（旧行为：`if (!toast)` 就什么都不显示）。
  if (!body) return list;
  const i = list.findIndex((x) => x.text === body);
  if (i >= 0) {
    const copy = list.slice();
    const [hit] = copy.splice(i, 1);
    return copy.concat([{ ...hit, at: now }]);
  }
  //: id 取"现有最大 + 1" —— 纯函数自己算，不靠外面的计数器
  //: （计数器在纯函数里等于把一个参数藏进了模块状态）。
  const nextId = list.reduce((m, x) => Math.max(m, x.id || 0), 0) + 1;
  const next = list.concat([{ id: nextId, text: body, at: now }]);
  return next.length > TOAST_MAX ? next.slice(next.length - TOAST_MAX) : next;
}

/** 扫掉过期的（每条按自己的 `at` 算）。 */
export function pruneToasts(list, now = Date.now()) {
  return list.filter((x) => now - x.at < TOAST_TTL_MS);
}
