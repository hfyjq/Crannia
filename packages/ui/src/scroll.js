// 「还贴着底吗」—— 对话区自动跟随的那条判据（纯函数，node 能直接断言）。
//
// ## 用户
//
// 「当用户主动上下滑动时，不要再把用户强行拉回底部了，除非用户自己滑回底部」。
//
// 旧行为：`messages` 每变一次就 `scrollIntoView`，用户往上翻着看历史，
// 模型吐一个字就把他拽回底部（读一页被打断十次）。
//
// 判据落在 "贴着底" 上，不是"有没有滚过"：
//   滚上去再滚回底部，照样跟随（用户自己回到了底部，这就是"要跟随"的意思）。
//   所以不存在"用户永久关掉跟随"这种状态，也不需要一个开关。
//
// 容差必须有：浏览器渲染的 `scrollTop` 是整数、`scrollHeight` 有小数，
//   贴底时 `scrollHeight - scrollTop - clientHeight` 常常是 1~2px 而不是 0。
//   写成 `=== 0` 会让"贴着底"永远判假 —— 表现是自动跟随彻底失灵。

/** 离底多少像素之内算「贴着底」（约两行文字）。 */
export const STICK_PX = 80;

/**
 * 这一屏是不是贴着底。
 *
 * 入参就是滚动元素的三个数（`scrollTop` / `scrollHeight` / `clientHeight`）——
 * 传对象而不是元素，是为了在 node 里不用 DOM 就能断言。
 */
export function isNearBottom(box, slack = STICK_PX) {
  if (!box) return true;                    // 还没挂上，当成贴着底（宁可跟随）
  const top = Number(box.scrollTop) || 0;
  const height = Number(box.scrollHeight) || 0;
  const view = Number(box.clientHeight) || 0;
  const gap = height - top - view;
  //:内容比视口短（没得滚），永远是"贴着底"。
  return gap <= slack;
}

/**
 * 滚动事件之后，`stick` 该变成什么 —— 把"内容变高"与"用户往上滑"分开。
 *
 * 用户的现场：步骤流里冒出几行长命令，内容一下子变高
 * 旧判据（"离底 > 80px 就当成用户滑走了"）在同一次重排里判成"用户跑了"
 *，自动跟随关掉、那颗「回到底部」按钮冒出来 —— 而用户一根手指都没动。
 * 点回去之后，下一条命令再来一次，又被顶上去。
 *
 * 判据：只有 `scrollTop` 变小才算"用户往上滑"（内容变高不会让 `scrollTop` 变小；
 *   程序化滚动只会让它变大）。其余情况一律只看"是不是又贴回底了"。
 *
 * 入参 / 出参都是纯值（`{stick, lastTop}` + 滚动元素的三个数），node 能直接断言。
 */
export function nextStick(state, box, slack = STICK_PX) {
  const prev = state || {};
  const top = Number(box && box.scrollTop) || 0;
  const near = isNearBottom(box, slack);
  const lastTop = Number(prev.lastTop) || 0;
  //:容差 2px：浏览器在内容变化时会把 scrollTop 抖动一两个像素。
  const movedUp = top < lastTop - 2;
  const stick = movedUp ? near : (near ? true : !!prev.stick);
  return { stick, lastTop: top };
}
