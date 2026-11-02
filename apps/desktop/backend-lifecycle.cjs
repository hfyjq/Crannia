// 后端子进程的生命周期 —— 三级降级停 + 退出前问一句。
//
// 与 dsh 的 `apps/desktop/src/host-process.ts` 对应（MIT，见 THIRD-PARTY.md
// 第 17 节）：三级超时值、退出前询问的上限、`inspectQuit` / `requireGraceful`
// 的语义都来自那份实现。
//
// 本文件不 `require("electron")`，也不认识后端是什么 ——
//    它只吃一个"像 child_process 的 child"和一个"能问状态的函数"。
//    于是 `tests/test_desktop_lifecycle.py` 能真的 spawn 几个 node 子进程
//    把它跑一遍（包括赖着不走那种）—— 证据要追到"哪一层被观察到"。
//
// 判据逐条：
//
//   ① 终止三级降级：优雅 shutdown → 10s → SIGTERM → 5s → SIGKILL → 5s → 抛
//   ② 退出前 `inspectQuit`：2 秒 deadline，超时/失败，按"有任务在跑"处理
//      （fail-safe：问不出来也当"有任务在跑"）
//   ③ 诊断信息有界（64KB）—— 理由：插件的 stderr 里可能混着凭据
//   ④ `requireGraceful`（更新交接专用）：exitCode 0 但没 ack shutdown 也算不干净
//
// 第一级的载体必须说清楚：Python 子进程没有 `process.send`
//    所以第一级是 HTTP（`POST /api/shutdown`，那边回一个 `ack: true`），
//    而不是 IPC（`child.send`）。降级链是三级，第一级的载体不是 IPC。
"use strict";

//: 默认的三级（毫秒）。10s / 5s / 5s 是有意定的数 —— 改动前先确认新值覆盖了哪一档等待。
const DEFAULT_STEPS = [
  { level: "graceful", ms: 10000 },
  { level: "SIGTERM", ms: 5000 },
  { level: "SIGKILL", ms: 5000 },
];

//: 诊断留多少 —— 上限 64KB。
const MAX_DIAGNOSTIC_CHARS = 64 * 1024;

/** 有界的诊断缓冲：留尾部，并记住丢了多少。
 *
 *  判据：这份诊断会进可展开的界面，而插件的 stderr 里可能混着凭据
 *，不设上限就等于把一份没人读得完、
 *  却可能带着密钥的东西留在内存里、还要往界面上摆。
 *
 *  丢了要说丢了（`dropped()`）—— 只留尾部而不出声，读的人会以为
 *  "日志就这么点"（判据：报数，别报字符）。
 */
function createDiagnostics(maxChars = MAX_DIAGNOSTIC_CHARS) {
  let text = "";
  let dropped = 0;
  return {
    push(chunk) {
      text += String(chunk);
      if (text.length > maxChars) {
        dropped += text.length - maxChars;
        text = text.slice(-maxChars);
      }
    },
    text: () => text,
    dropped: () => dropped,
  };
}

/** 等 `child` 退出，最多 `ms` 毫秒。已经退了就立刻返回 true。
 *
 *  判据只能是 `exitCode !== null` / `signalCode !== null`（进程真的没了），
 *  不是"我们发过信号"—— 发出去不等于它走了（替身测试里
 *  `exit(17)` 与 `exit(0)` 是两个用例，因为"退得不干净"与"没退"是两回事）。
 */
function waitForExit(child, ms) {
  if (!child) return Promise.resolve(true);
  if (child.exitCode !== null && child.exitCode !== undefined) return Promise.resolve(true);
  if (child.signalCode !== null && child.signalCode !== undefined) return Promise.resolve(true);
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (done) return; done = true; clearTimeout(timer); resolve(v); };
    const timer = setTimeout(() => finish(false), ms);
    child.once("exit", () => finish(true));
  });
}

/** 三级降级停一个子进程。
 *
 *  @param {object} child                `child_process` 的 ChildProcess（可以为 null）。
 *  @param {object} [opts]
 *  @param {Function} [opts.graceful]    第一级：请它自己退（`async () => {}`）。
 *                                       不给，跳过第一级（直接 SIGTERM）。
 *  @param {Array}  [opts.steps]         覆盖三级表（测试用）。
 *  @param {Function} [opts.waitForExit] 换掉等待器（测试用）。
 *  @returns {Promise<{ok:boolean, level:string, waitedMs:number}>}
 */
async function stopChild(child, opts = {}) {
  const steps = opts.steps || DEFAULT_STEPS;
  const wait = opts.waitForExit || waitForExit;
  const t0 = Date.now();
  if (!child) return { ok: true, level: "no-process", waitedMs: 0 };
  if (await wait(child, 0)) return { ok: true, level: "already-exited", waitedMs: 0 };

  for (const step of steps) {
    if (step.level === "graceful") {
      if (!opts.graceful) continue;
      // 发不出去（连接被拒 / 后端已经半死）不算失败 —— 那正是要往下走一级的信号。
      try {
        await opts.graceful();
      } catch (err) {
        console.warn(`[stop] 优雅那一级没送到：${err && err.message ? err.message : err}`);
      }
    } else {
      const sig = step.level;
      try {
        child.kill(sig);
      } catch (err) {
        console.warn(`[stop] ${sig} 发不出去：${err && err.message ? err.message : err}`);
      }
    }
    if (await wait(child, step.ms)) {
      return { ok: true, level: step.level, waitedMs: Date.now() - t0 };
    }
  }
  return { ok: false, level: "failed", waitedMs: Date.now() - t0 };
}

/** 给一个 promise 套上 deadline。超时抛，由调用方决定怎么兜。 */
function withDeadline(promise, ms) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${ms}ms 内没有回音`)), ms);
    Promise.resolve(promise).then(
      (v) => { clearTimeout(timer); resolve(v); },
      (e) => { clearTimeout(timer); reject(e); },
    );
  });
}

/** 退出前问一句：现在退，会打断什么吗？
 *
 *  判据：2 秒 deadline；超时/失败，按"有任务在跑"处理。
 *  这条 fail-safe 的方向是承重的：它反过来写（问不出来，当没事）时，
 *  "后端卡死"与"一切正常"会让壳做出同一个动作（直接退），
 *  而前者恰恰是最需要问一句的那一种。
 *
 *  @param {Function} getStatus  `async () => ({agent_busy, agent_task})`
 *  @param {number}   [deadlineMs]
 *  @returns {Promise<{busy:boolean, reason:string}>} —— 永不抛。
 */
async function inspectQuit(getStatus, deadlineMs = 2000) {
  let status;
  try {
    status = await withDeadline(getStatus(), deadlineMs);
  } catch (err) {
    return { busy: true, reason: `问不出来（${err && err.message ? err.message : err}）` };
  }
  // 读不到 ≠ 没在跑（读不到一律不当 0），一样按 busy 处理。
  if (!status || typeof status !== "object") return { busy: true, reason: "没读出状态" };
  if (status.agent_busy) {
    return { busy: true, reason: `正在跑：${status.agent_task || "（它没报名字）"}` };
  }
  // 挂着一条等你答的审批也是"有活在跑" —— 退出会把那条审批连同
  //    等着它的那一轮一起丢掉，而 `agent_busy` 在"挂起即停车"那一刻已经是 false。
  //    那一格缺失时按"有"处理（读不到 ≠ 没有）——
  //    它与 `""`（确实没有）是两件事，合成一个真值判断就把这条判据废掉了。
  const pend = status.approval_pending;
  if (pend === undefined || pend === null) {
    return { busy: true, reason: "状态里没有待审批那一格（按有处理）" };
  }
  if (pend) {
    return {
      busy: true,
      reason: pend === "unknown"
        ? "读不出待审批的状态（按有处理）"
        : `挂着一条待审批：${pend}`,
    };
  }
  return { busy: false, reason: "" };
}

/** `requireGraceful` 那一档（更新交接专用）。
 *
 *  判据：exitCode 0 但没 ack shutdown 也算不干净。
 *，干净 = 我们请过它（走过 `graceful` 那一级）且它退得没毛病。
 *  反过来说：这一档不接受"被人杀了但退出码恰好是 0"。
 */
function isCleanExit(stopResult, request) {
  const asked = Boolean(request && request.ack);
  return Boolean(stopResult && stopResult.ok) && stopResult.level !== "SIGKILL"
    && stopResult.level !== "SIGTERM" && asked;
}

module.exports = {
  DEFAULT_STEPS,
  MAX_DIAGNOSTIC_CHARS,
  createDiagnostics,
  waitForExit,
  stopChild,
  withDeadline,
  inspectQuit,
  isCleanExit,
};
