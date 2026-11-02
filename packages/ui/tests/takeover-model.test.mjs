// `packages/ui/src/takeover-model.js` 的读者（`W179`，2026-10-18）。
//
//   node packages/ui/tests/takeover-model.test.mjs
//
// 钉的是三句界面会说出口的话（标签在说谎）：
//   ① 按钮什么时候可点、不可点时那句悬停提示是什么；
//   ② 按钮上写什么；
//   ③ 退出接管时给用户哪一句原因（不认识的原因不许编）。

import assert from "node:assert/strict";
import {
  stopReasonText, takeoverAvailability, takeoverLabel,
} from "../src/takeover-model.js";

let pass = 0;
const failed = [];
function check(label, ok, note = "") {
  if (ok) { pass++; return; }
  failed.push(`${label}${note ? " -- " + note : ""}`);
}

// ── ① 可不可点 ───────────────────────────────────────────────
{
  const no = takeoverAvailability({ mode: "manual", connected: true, enabled: false, desktop: true });
  check("非完全权限 -> 不可点", no.ok === false);
  check("★ 不可点时**必须有理由**（用户逐字：悬停提示需要更高权限）",
        /完全权限/.test(no.reason), no.reason);

  const off = takeoverAvailability({ mode: "full_access", connected: false, enabled: false, desktop: true });
  check("服务没连上 -> 不可点", off.ok === false);

  const yes = takeoverAvailability({ mode: "full_access", connected: true, enabled: false, desktop: true });
  check("完全权限 + 已连接 -> 可点", yes.ok === true);

  const on = takeoverAvailability({ mode: "auto", connected: true, enabled: true, desktop: true });
  check("★ 已经开着 -> **永远可点**（那是停用，不是启用）", on.ok === true, JSON.stringify(on));

  const web = takeoverAvailability({ mode: "full_access", connected: true, enabled: false, desktop: false });
  check("★ 浏览器里（没有顶层横幅）-> 不可点，且理由说清",
        web.ok === false && /桌面版/.test(web.reason), JSON.stringify(web));
}

// ── ② 按钮字 ─────────────────────────────────────────────────
{
  check("没开 -> 接管", takeoverLabel({ enabled: false }) === "接管");
  check("开着 -> 接管中", takeoverLabel({ enabled: true }) === "接管中");
  check("拿不到状态 -> 接管（fail-closed 到「没开」那一侧）",
        takeoverLabel(null) === "接管");
}

// ── ③ 退出原因 ───────────────────────────────────────────────
{
  check("物理键盘", /键盘/.test(stopReasonText("physical_key")));
  check("物理鼠标", /鼠标/.test(stopReasonText("physical_mouse")));
  check("Esc", /Esc/.test(stopReasonText("esc")));
  check("托盘", /托盘/.test(stopReasonText("tray")));
  check("模式切走", /模式/.test(stopReasonText("mode=auto")));
  check("★ 不认识的原因 -> **空串**（不许编一句）", stopReasonText("weird") === "");
  check("空原因 -> 空串", stopReasonText("") === "");
  check("null -> 空串", stopReasonText(null) === "");
}

if (failed.length) {
  console.log(failed.map((f) => "FAIL  " + f).join("\n"));
  console.log(`\n${pass}/${pass + failed.length} passed`);
  process.exit(1);
}
console.log(`${pass}/${pass} passed`);
