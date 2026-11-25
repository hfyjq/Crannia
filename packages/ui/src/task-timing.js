// 任务浮窗里的用时 —— 纯函数，认识普通对象、不认识 React。
//
// `/api/tasks` 的 `elapsed_ms` 是"拉取那一刻"的读数；面板只在打开与事件
// 到达时拉一次，于是开着的面板里那个数原地不动（点开一次、时间就冻住）。
// 这里做的是把服务端快照当基准，本地按秒往上推；下一次事件重拉列表时，
// 基准跟着换成新的快照。不新增轮询。
//
// 单独一个文件的理由与 `format.js` 相同：本包没有 vitest/jest，行为断言
// 用 node 直接跑（`packages/ui/tests/task-timing.test.mjs`，
// Python 那侧是 `tests/test_desktop_task_timing.py`）。

/** 一条任务的当前用时（毫秒）。
 *
 *  `fetchedAt` = `/api/tasks` 响应到达的那一刻（前端记的）；
 *  `now` = 渲染这一刻。漂移只取 `now - fetchedAt` 的正数部分，
 *  系统时钟往回跳时不会出现"越跑越少"。
 *  已结束的条目不再走秒：结束时间由服务端给的那一份说了算。
 */
export function liveElapsedMs(task, fetchedAt, now) {
  const base = Number(task && task.elapsed_ms) || 0;
  if (!task || task.status !== "running") return base;
  const at = Number(fetchedAt) || 0;
  if (!at) return base;
  return base + Math.max(0, Number(now) - at);
}
