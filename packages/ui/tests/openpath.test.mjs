// `packages/ui/src/openpath.js` 的读者 —— 零依赖、node 直接跑。
//   node packages/ui/tests/openpath.test.mjs
//
// 它决定哪个链接走哪扇门（网址 -> window.open / 本机路径 -> /api/reveal），
//    所以边界要钉死：判错方向的代价是"把一个不该给系统浏览器的东西递过去"。
// Python 那侧由 `tests/test_desktop_openpath.py` 接进全量 runner。

import { isWebUrl, setPathOpener, openLocalPath } from "../src/openpath.js";

let pass = 0;
const failed = [];
function check(label, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; return; }
  failed.push(label + ": got " + JSON.stringify(got) + ", want " + JSON.stringify(want));
}

// -- web URLs --
check("http", isWebUrl("http://a.com/x"), true);
check("https", isWebUrl("https://a.com/x"), true);
check("mailto", isWebUrl("mailto:a@b.c"), true);
check("protocol-relative //", isWebUrl("//cdn.a.com/x.js"), true);
check("case-insensitive", isWebUrl("HTTP://A.COM"), true);
check("trims whitespace", isWebUrl("  https://a.com  "), true);

// -- local paths (this group is load-bearing) --
//:反斜杠不用字面量写（今天实测：写进这个文件的路上会被吃掉一层，
//:    于是 `\x` 变成非法的十六进制转义、整个文件加载不了）。
//: 用 `String.fromCharCode(92)` 拼 —— 绕开所有转义层。
const BS = String.fromCharCode(92);
check("windows absolute", isWebUrl("D:" + BS + "x" + BS + "x" + BS + "report.docx"), false);
check("forward-slash absolute", isWebUrl("D:/x/report.docx"), false);
check("workspace-relative", isWebUrl("workspace/alpha/report.docx"), false);
check("./ prefix", isWebUrl("./a.docx"), false);
check("bare filename", isWebUrl("report.docx"), false);
check("in-page anchor (treated as path)", isWebUrl("#x"), false);
check("empty is not a url", isWebUrl(""), false);
check("null is not a url", isWebUrl(null), false);

// -- the door --
let got = [];
setPathOpener((h) => { got.push(h); return "ok"; });
await openLocalPath("  D:/a.docx  ");
check("registered: called, and trimmed", got, ["D:/a.docx"]);

setPathOpener(null);
check("not registered (WebUI in a browser) => silently no-op, never throws",
      await openLocalPath("D:/a.docx"), false);

setPathOpener(() => { throw new Error("boom"); });
let threw = false;
try { await openLocalPath("D:/a.docx"); } catch { threw = true; }
check("opener throws => propagated (the shell must tell the user)", threw, true);

console.log("openpath.js self-check: " + pass + " pass / " + failed.length + " fail");
for (const f of failed) console.log("  FAIL " + f);
process.exit(failed.length ? 1 : 0);
