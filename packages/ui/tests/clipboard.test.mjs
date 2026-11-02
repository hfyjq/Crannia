// `packages/ui/src/clipboard.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/clipboard.test.mjs
//
// 为什么这一格必须有读者：它是本仓那个"我判不了"的格子 ——
//   写剪贴板在渲染进程里到底行不行，取决于安全上下文与该版本 Chromium
//   要不要权限，而那要实际点一下才知道（写代码的人做不了）。
//   所以`copyText` 把 `document` / `navigator` 做成参数，于是三条路
//   可以各喂一份假货，在真 node 里逐条走一遍。
//
// 判据是返回值，不是"调用过谁" —— `copyText` 的契约就是
//   `true` = 真的进了剪贴板、`false` = 没成（`W44`：没出声与没跑必须分得开）。

import assert from "node:assert/strict";
import { copyText } from "../src/clipboard.js";

let pass = 0;
const failed = [];
function check(label, ok, note = "") {
  if (ok) { pass++; return; }
  failed.push(`${label}${note ? " -- " + note : ""}`);
}

// ── 假货：`navigator.clipboard.writeText` 那一档 ─────────────────────
function fakeNav({ ok = true, throws = false, hasApi = true } = {}) {
  const calls = [];
  const nav = hasApi
    ? { clipboard: { writeText: async (s) => { calls.push(s); if (throws) throw new Error("denied"); return ok; } } }
    : {};
  return { nav, calls };
}

// ── 假货：`document.execCommand` 那一档 ─────────────────────────────
function fakeDoc({ copyOk = true, hasExec = true } = {}) {
  const seen = { appended: 0, removed: 0, lastValue: "", selected: false };
  const node = {
    value: "", style: {},
    setAttribute() {},
    select() { seen.selected = true; },
  };
  const doc = {
    body: { appendChild() { seen.appended++; }, removeChild() { seen.removed++; } },
    createElement() { return node; },
  };
  if (hasExec) doc.execCommand = (cmd) => { seen.lastValue = node.value; return copyOk && cmd === "copy"; };
  return { doc, seen, node };
}

// ── ① 标准路能用，走它，且不去碰 execCommand ──────────────────
{
  const { nav, calls } = fakeNav();
  const { doc, seen } = fakeDoc();
  const ok = await copyText("hello", doc, nav);
  check("★ ① 标准路能用 -> 返回 true", ok === true, String(ok));
  check("★ ① 而它真的把内容交给了 `writeText`", calls.length === 1 && calls[0] === "hello",
        JSON.stringify(calls));
  check("★ ① 标准路成功时**不碰** `execCommand`（别多写一遍剪贴板）",
        seen.appended === 0, JSON.stringify(seen));
}

// ── ② 标准路抛错，落到老路，而且要真的复制成功 ─────────────────
{
  const { nav } = fakeNav({ throws: true });
  const { doc, seen } = fakeDoc();
  const ok = await copyText("fallback", doc, nav);
  check("★★ ② 标准路抛错 -> 落到老路，并返回 true（**不是**一抛就认输）", ok === true, String(ok));
  check("★ ② 老路真的把那段文本放进了那个 textarea",
        seen.lastValue === "fallback" && seen.selected, JSON.stringify(seen));
  check("★★ ② 而且造出来的东西**拆干净了**（不许在 body 里留垃圾）",
        seen.appended === 1 && seen.removed === 1, JSON.stringify(seen));
}

// ── ③ 标准路不在（老浏览器 / 权限被挡），老路 ──────────────────
{
  const { nav } = fakeNav({ hasApi: false });
  const { doc } = fakeDoc();
  check("★ ③ 没有 `navigator.clipboard` -> 老路接手，true",
        (await copyText("x", doc, nav)) === true, "");
}

// ── ④ 三条都不行，返回 false（不许装成功）──────────────────
{
  const { nav } = fakeNav({ hasApi: false });
  const { doc } = fakeDoc({ hasExec: false });
  check("★★ ④ 两条路都没有 -> **false**（这一条是本文件的重点：不许静默当成功）",
        (await copyText("x", doc, nav)) === false, "");
  const { nav: nav2 } = fakeNav({ hasApi: false });
  const { doc: doc2 } = fakeDoc({ copyOk: false });
  check("★★ ④ `execCommand` 自己回 false -> **false**（也不许当成功）",
        (await copyText("x", doc2, nav2)) === false, "");
}

// ── ⑤ 空内容，false，且一个字节都不写 ────────────────────────
{
  const { nav, calls } = fakeNav();
  const { doc, seen } = fakeDoc();
  check("★★ ⑤ 空串 / `null` / `undefined` -> false（写进去等于**清掉用户的剪贴板**）",
        (await copyText("", doc, nav)) === false
        && (await copyText(null, doc, nav)) === false
        && (await copyText(undefined, doc, nav)) === false, "");
  check("★ ⑤ 而那三次**都没碰剪贴板**", calls.length === 0 && seen.appended === 0,
        JSON.stringify({ calls, appended: seen.appended }));
}

// ── ⑥ `execCommand` 自己抛，也返回 false（不把异常扔给调用方）────
{
  const { nav } = fakeNav({ hasApi: false });
  const doc = {
    body: { appendChild() {}, removeChild() {} },
    createElement() { return { value: "", style: {}, setAttribute() {}, select() {} }; },
    execCommand() { throw new Error("not supported"); },
  };
  let threw = null;
  let ok = null;
  try { ok = await copyText("x", doc, nav); } catch (e) { threw = e; }
  check("★★ ⑥ `execCommand` 抛错 -> 返回 false，**不把异常扔给调用方**",
        threw === null && ok === false, threw ? String(threw.message) : String(ok));
}

// ── ⑦ `document` / `navigator` 缺省时不许炸（现场那两条是全局）────
{
  let threw = null;
  let ok = null;
  try { ok = await copyText("x", null, null); } catch (e) { threw = e; }
  check("★ ⑦ 两个都传 null（模拟「啥都没有的环境」）-> 返回 false，不炸",
        threw === null && ok === false, threw ? String(threw.message) : String(ok));
}

if (failed.length) {
  console.log("FAILED:");
  for (const f of failed) console.log("  - " + f);
}
console.log(`${pass + failed.length} tests: ${pass} passed, ${failed.length} failed`);
assert.equal(failed.length, 0);
