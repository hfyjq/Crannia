// 把一段文本送进系统剪贴板。
//
// 它为什么单独一个文件、又为什么把 `document`/`navigator` 做成参数
//
// 因为这一层的结果在编码时无法判定：`navigator.clipboard?.writeText(...)`
// 那种写法里，那个 `?.` 让失败也静默通过 —— 「不能用」与「用了、没成」
// 在代码上长得一模一样。
//
// 所以不去赌哪一条路能用，三条按序试，结果如实返回。
//   ① `navigator.clipboard.writeText` —— 标准路（要安全上下文；本壳的
//      `crannia-app://` 在 `registerSchemesAsPrivileged` 里开了 `secure`，
//      理论上满足 —— 但"理论上"不是判据，所以下面还有两条）。
//   ② `document.execCommand("copy")` —— 老路，不需要任何权限，
//      Chromium 至今没删（废弃但可用）。
//   ③ 都不行，返回 `false`（不许装成功）。
//
// `document`/`navigator` 做成参数、有默认值，是为了能被 node 钉：
//   把依赖注进来，纯函数就能在真 node 里跑（同 `provider-icons.js` 那条先例）。
//   所以读者 `packages/ui/tests/clipboard.test.mjs`。
//
// 它只负责"写" —— 粘贴（读剪贴板）不走这里：
//   读剪贴板在 Chromium 里要权限，而右键菜单那条路由壳侧原生实现
//   （`apps/desktop/main.cjs` 的 `role:"paste"`），那条路根本不经过渲染进程。

/** 往剪贴板里写一段文本。返回 `true` / `false`，不抛 —— 调用方要能分开
 *  「复制了」与「没复制」（没出声与没跑必须分得开）。
 *
 *  @param {unknown} text 要写的内容（`null` / `undefined`，`false`，不写空的）。
 *  @param {object} [doc] `document`（注入是为了能在 node 里跑）。
 *  @param {object} [nav] `navigator`（同上）。
 *  @returns {Promise<boolean>}
 */
export async function copyText(text, doc, nav) {
  const s = String(text == null ? "" : text);
  if (!s) return false;                       // 空的不写（写进去等于把用户的剪贴板清掉）
  const d = doc || (typeof document !== "undefined" ? document : null);
  const n = nav || (typeof navigator !== "undefined" ? navigator : null);

  // ① 标准路
  try {
    if (n && n.clipboard && typeof n.clipboard.writeText === "function") {
      await n.clipboard.writeText(s);
      return true;
    }
  } catch {
    // 落到 ② —— 这里刻意不打印：它是一条会走到下一步的失败，
    // 不是结论。真不行的结论由返回值说（调用方看得见）。
  }

  // ② 老路：造一个看不见的 textarea，选中、复制、拆掉。
  try {
    if (d && d.body && typeof d.execCommand === "function") {
      const ta = d.createElement("textarea");
      ta.value = s;
      ta.setAttribute("readonly", "");         // 不弹键盘、不改输入法状态
      ta.style.position = "fixed";
      ta.style.top = "-1000px";                // 挪出可视区（`display:none` 在部分实现上选不中）
      ta.style.opacity = "0";
      d.body.appendChild(ta);
      ta.select();
      const ok = d.execCommand("copy");
      d.body.removeChild(ta);
      return !!ok;
    }
  } catch {
    // ③ 落到"没成"
  }
  return false;
}
