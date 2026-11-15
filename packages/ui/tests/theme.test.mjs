// `packages/ui/src/theme.js` 的读者 —— 零依赖、node 直接跑。
//
//   node packages/ui/tests/theme.test.mjs
//
// Python 那一侧由 `tests/test_desktop_theme.py` 接进全量 runner
//   （同 `mode.test.mjs` / `test_desktop_mode.py` 那一对）。
//
// 判据（步骤②）：
//   ① 没自定义，一个字节都不注入（默认观感不许变，这是步骤①的对账之后
//      唯一还守得住的东西 —— 一旦默认就注入，等于把 658/658 白对了）；
//   ② 坏值只丢那一格，不整份失效（同"读不到，默认"）；
//   ③ 六个轴的名字与默认值必须和 `styles.css` 对得上（这里只钉 JS 这一侧，
//      CSS 那一侧由 `tests/test_desktop_theme.py` 反查）。

import assert from "node:assert/strict";
import {
  AXES, THEME_KEY, DARK_THEME_KEY, DARK_DEFAULTS, MODE_KEY, THEME_MODES,
  allDefaults, defaultsFor, isHex, normalize, changedCount,
  loadTheme, saveTheme, clearTheme, themeCss, applyTheme,
  loadMode, saveMode, resolveMode, applyMode,
} from "../src/theme.js";

let pass = 0;
const failed = [];
function check(label, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; return; }
  failed.push(`${label}: 得到 ${JSON.stringify(got)}，期望 ${JSON.stringify(want)}`);
}
function ok(label, cond) {
  if (cond) { pass++; return; }
  failed.push(`${label}: 条件不成立`);
}

function fakeStorage(seed) {
  const m = new Map(Object.entries(seed || {}));
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => { m.set(k, String(v)); },
    removeItem: (k) => { m.delete(k); },
    _map: m,
  };
}

function fakeDoc() {
  const nodes = [];
  return {
    head: { appendChild: (el) => nodes.push(el) },
    createElement: (tag) => {
      const el = { tag, id: "", textContent: "" };
      el.remove = () => { const i = nodes.indexOf(el); if (i >= 0) nodes.splice(i, 1); };
      return el;
    },
    getElementById: (id) => nodes.find((n) => n.id === id) || null,
    nodes,
  };
}

// ── 表本身 ─────────────────────────────────────────────────────────
check("★ 六个轴、顺序固定",
  AXES.map((a) => a.key).join(","),
  "sideBg,sideFg,appBg,boxBg,boxFg,select");
check("★ 六个 CSS 变量名",
  AXES.map((a) => a.cssVar).join(","),
  "--th-side-bg,--th-side-fg,--th-app-bg,--th-box-bg,--th-box-fg,--th-select");
ok("★ 每个默认值都是合法 #rrggbb", AXES.every((a) => isHex(a.def)));
check("★ 存储键是 GUI 自己的（`lv.` 前缀，同 `lv.sideW`）", THEME_KEY, "lv.colors");

// ── 校验 / 归一 ─────────────────────────────────────────────────────
ok("合法六位十六进制收", isHex("#a1b2c3") && isHex("#A1B2C3"));
ok("❌ 三位、八位、rgb()、空、数字都不收",
  !isHex("#abc") && !isHex("#a1b2c3d4") && !isHex("rgb(1,2,3)")
  && !isHex("") && !isHex(123456));
check("★ 坏值只丢那一格，其余照留",
  normalize({ sideBg: "#112233", sideFg: "nope", evil: "#000000" }),
  { sideBg: "#112233" });
check("★ 大写归一成小写", normalize({ boxBg: "#ABCDEF" }), { boxBg: "#abcdef" });
check("★★ 非对象 / null ⇒ 空（不抛）", normalize(null), {});
check("★★ `allDefaults()` 六格齐全", Object.keys(allDefaults()).length, 6);

// ── 存 / 读 / 清 ────────────────────────────────────────────────────
const st = fakeStorage();
check("★ 没存过 ⇒ 空（用默认值）", loadTheme(st), {});
check("★ 坏的 JSON ⇒ 空（不抛）",
  loadTheme(fakeStorage({ [THEME_KEY]: "{not json" })), {});
ok("★ 存得进", saveTheme({ sideBg: "#010203", appBg: "#040506" }, st));
check("★ 读得回", loadTheme(st), { sideBg: "#010203", appBg: "#040506" });
saveTheme({ sideBg: "#010203", junk: "#ffffff" }, st);
check("★★ 存的时候也只留认识的键",
  JSON.parse(st.getItem(THEME_KEY)), { sideBg: "#010203" });
clearTheme(st);
check("★ 清掉之后回到空", loadTheme(st), {});

// ── 改过几格 ────────────────────────────────────────────────────────
check("★ 全默认 ⇒ 0 格改过", changedCount(allDefaults()), 0);
check("★ 改一格 ⇒ 1", changedCount({ ...allDefaults(), select: "#123456" }), 1);
check("★ 传入的正是默认值 ⇒ 不算改过",
  changedCount({ sideBg: AXES[0].def }), 0);

// ── 生成的 CSS ──────────────────────────────────────────────────────
check("★★★ 没自定义 ⇒ **空串**（一个字节都不注入；默认观感不许变）",
  themeCss({}), "");
const css = themeCss(allDefaults());
ok("★ 六格都写进 :root", AXES.every((a) => css.includes(`${a.cssVar}: ${a.def};`)));
ok("★ 默认值下**不加** `.sidebar{color:…}`（否则改的是继承色）",
  !css.includes(".sidebar{color"));
const css2 = themeCss({ ...allDefaults(), sideFg: "#ffffff" });
ok("★ 改了左边栏文字 ⇒ 补上 `.sidebar{color:var(--th-side-fg)}`",
  css2.includes(".sidebar{color:var(--th-side-fg)}"));
const css3 = themeCss({ appBg: "#000000" });
ok("★★ 只存一格时，其余五格**补默认值**（不让它们变空）",
  AXES.filter((a) => a.key !== "appBg")
    .every((a) => css3.includes(`${a.cssVar}: ${a.def};`)));

// ── 注入文档 ────────────────────────────────────────────────────────
const doc = fakeDoc();
applyTheme({}, doc);
check("★★ 空配色 ⇒ 不创建任何元素", doc.nodes.length, 0);
applyTheme({ appBg: "#101010" }, doc);
check("★ 注入了**一个** style", doc.nodes.length, 1);
check("★ 它的 id 是 `lv-theme`", doc.nodes[0].id, "lv-theme");
ok("★ 内容真的进去了", doc.nodes[0].textContent.includes("--th-app-bg: #101010"));
applyTheme({ appBg: "#202020" }, doc);
check("★★ 再注入一次是**替换**，不是再插一个", doc.nodes.length, 1);
ok("★ 内容跟着换", doc.nodes[0].textContent.includes("#202020"));
applyTheme({}, doc);
check("★★ 恢复默认 ⇒ 元素被摘掉（回到样式表原值）", doc.nodes.length, 0);

// ── 深色：两套默认值、两套存储、三种模式 ─────────────────────────────
check("★ 深色六个默认值齐全", Object.keys(DARK_DEFAULTS).length, 6);
ok("★ 深色每个默认值都是合法 #rrggbb",
  Object.values(DARK_DEFAULTS).every((v) => isHex(v)));
ok("★ 深浅两套默认值**逐格不同**（否则等于没做深色）",
  AXES.every((a) => DARK_DEFAULTS[a.key] !== a.def));
check("★ defaultsFor('dark') 与表对齐",
  Object.keys(defaultsFor("dark")).join(","), "sideBg,sideFg,appBg,boxBg,boxFg,select");
check("★★ themeCss({}, 'dark') 同样是空串（没自定义不注入）", themeCss({}, "dark"), "");
const dcss = themeCss({ sideFg: "#ffffff" }, "dark");
ok("★★ 深色下补的是**深色默认值**，不是浅色那份",
  dcss.includes(`--th-app-bg: ${DARK_DEFAULTS.appBg};`)
  && !dcss.includes(`--th-app-bg: ${AXES[2].def};`));
ok("★ 深色下改了左边栏文字，同样补 `.sidebar` 规则（比的是深色默认值）",
  dcss.includes(".sidebar{color:var(--th-side-fg)}"));

const st2 = fakeStorage();
check("★ 模式缺省 = 跟随系统", loadMode(st2), "system");
saveMode("dark", st2);
check("★ 写进去读得回", loadMode(st2), "dark");
saveMode("nonsense", st2);
check("★ 认不出的值 ⇒ 按跟随系统（不抛、不记坏值）", loadMode(st2), "system");

check("★★ system + 系统深色 ⇒ dark", resolveMode("system", { matches: true }), "dark");
check("★★ system + 系统浅色 ⇒ light", resolveMode("system", { matches: false }), "light");
check("★ 手动那一档**不受系统影响**",
  resolveMode("light", { matches: true }), "light");
check("★ 拿不到 matchMedia ⇒ 按浅色", resolveMode("system", null), "light");

const edoc = { documentElement: { dataset: {}, style: {} } };
applyMode("dark", edoc);
check("★★ applyMode 打上 data-theme=dark", edoc.documentElement.dataset.theme, "dark");
check("★★ 并且设了 color-scheme（原生控件跟着变）",
  edoc.documentElement.style.colorScheme, "dark");
applyMode("light", edoc);
check("★ 切回浅色", edoc.documentElement.dataset.theme, "light");

const st3 = fakeStorage();
saveTheme({ appBg: "#010101" }, st3, "light");
saveTheme({ appBg: "#0a0a0a" }, st3, "dark");
check("★★ 两套各存各的（浅色键不变，深色写另一个键）",
  [JSON.parse(st3.getItem(THEME_KEY)), JSON.parse(st3.getItem(DARK_THEME_KEY))],
  [{ appBg: "#010101" }, { appBg: "#0a0a0a" }]);
check("★ 按模式读回来是对的",
  [loadTheme(st3, "light"), loadTheme(st3, "dark")],
  [{ appBg: "#010101" }, { appBg: "#0a0a0a" }]);
clearTheme(st3, "dark");
check("★ 恢复默认只清当前那一档", loadTheme(st3, "dark"), {});
check("★ 另一档原样留着", loadTheme(st3, "light"), { appBg: "#010101" });
check("★ 三种模式的名字固定",
  THEME_MODES.map((m) => m.key).join(","), "light,dark,system");
check("★ 模式存储键是 GUI 自己的", MODE_KEY, "lv.theme");

// ── 汇总 ────────────────────────────────────────────────────────────
if (failed.length) {
  console.error(`\n${failed.length} failed / ${pass + failed.length}`);
  for (const f of failed) console.error("  - " + f);
  process.exit(1);
}
console.log(`${pass} passed`);
