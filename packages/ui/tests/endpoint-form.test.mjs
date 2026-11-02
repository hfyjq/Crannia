// 端配置页判据的纯函数读者—— 零依赖，node 直接跑。
//
//   跑法： node packages/ui/tests/endpoint-form.test.mjs
//   （`tests/test_desktop_endpoint.py` 会跑它；也可以在 `npm test` 那一族里手跑。）
//
// 这里钉的是前端那一半的边界：api 该不该发、窗口填没填、按钮该不该灰。
//   判据的权威在后端（`core/model_configs.py`），所以两边的上限都由参数传进来
//   —— 这个文件里不许出现任何一个写死的上限（写死就是第二份真相源）。

import assert from "node:assert/strict";
import {
  apiHint, blankConfig, buildCreatePayload, buildUpdatePayload, canAdd,
  capModeVerdict, cardSubtitle, entryToForm, testVerdict, validateConfig,
  windowVerdict,
} from "../src/endpoint-form.js";

let failed = [];
function check(label, ok, note = "") {
  if (ok) {
    console.log(`  [ok]   ${label}`);
  } else {
    failed.push(label);
    console.log(`  [FAIL] ${label}${note ? "  -- " + note : ""}`);
  }
}

const LIMITS = { maxPerCard: 10, maxName: 40, maxModel: 120, maxUrl: 300,
                 windowMin: 1024, windowMax: 10000000 };

// ── 1. 空表与上限 ─────────────────────────────────────────────────────
check("blankConfig 的「高级选项」**默认收着**（用户「不清楚就别改」那句话要求它是要点开的）",
      blankConfig("gpt").advanced === false);
check("blankConfig 带上了卡片 id", blankConfig("glm").card === "glm");
check("blankConfig 的 api 是空串（新建时用户自己填）", blankConfig().api === "");

// ── 2. 上下文容量那一格 ───────────────────────────────────────────────
check("★ 留空是**合法**的（= 交给查表/实测那几层，不是错）",
      windowVerdict("", LIMITS).ok && windowVerdict("", LIMITS).value === null);
check("★ 填了就是它", windowVerdict("200000", LIMITS).value === 200000);
check("★ 非整数 -> 拒（且叫用户留空）",
      !windowVerdict("abc", LIMITS).ok && /留空/.test(windowVerdict("abc", LIMITS).error));
check("★ 越界 -> 拒", !windowVerdict("1", LIMITS).ok);
check("★ 上限**由参数给**（不是这个文件写死的 10,000,000）",
      windowVerdict("999", { windowMin: 1, windowMax: 1000 }).ok);

// ── 3. 整表判据 ───────────────────────────────────────────────────────
check("模型名不能空", !validateConfig({ ...blankConfig("gpt") }, LIMITS).ok);
check("名字超限 -> 拒",
      !validateConfig({ ...blankConfig("gpt"), name: "n".repeat(41), model: "m" }, LIMITS).ok);
check("地址不是 http(s) -> 拒",
      !validateConfig({ ...blankConfig("gpt"), model: "m", base_url: "ftp://x" }, LIMITS).ok);
check("★ 地址**留空是合法的**（用那家自带的地址）",
      validateConfig({ ...blankConfig("gpt"), model: "m" }, LIMITS).ok);

// 收着高级选项时，不拿一个看不见的值去拦提交
const hidden = { ...blankConfig("gpt"), model: "m", window: "abc", advanced: false };
check("★★ 高级选项收着时，窗口那格填了乱码也**不拦**（那一格用户根本看不见）",
      validateConfig(hidden, LIMITS).ok, JSON.stringify(validateConfig(hidden, LIMITS)));
const shown = { ...hidden, advanced: true };
check("★ 而展开之后它就拦了", !validateConfig(shown, LIMITS).ok);

// ── 4. 请求体 ─────────────────────────────────────────────────────────
const form = { ...blankConfig("gpt"), name: " A ", model: " gpt-4o ",
               base_url: " https://relay.example/v1 ", api: " sk-1 " };
const create = buildCreatePayload(form, LIMITS);
check("★ 建配置：四样都带上，且**两头空白被去掉**",
      create && create.name === "A" && create.model === "gpt-4o" &&
      create.base_url === "https://relay.example/v1" && create.api === "sk-1",
      JSON.stringify(create));
check("★ 高级选项收着 -> `window` 发 **null**（= 交给后端那几层）",
      create.window === null, JSON.stringify(create.window));
check("★ 不合法 -> 回 **null**（不发一个注定 400 的请求）",
      buildCreatePayload({ ...blankConfig("gpt") }, LIMITS) === null);

// 这条是本文件最要紧的一条
const upd = buildUpdatePayload("m123", { ...form, api: "" }, LIMITS);
check("★★ 改配置时 `api` **留空就不发那一格** —— 后端据此一个字不动那格凭据",
      upd && !("api" in upd), JSON.stringify(upd));
const upd2 = buildUpdatePayload("m123", { ...form, api: " sk-new " }, LIMITS);
check("★★ 而填了就发（换 key 那条路）", upd2.api === "sk-new", JSON.stringify(upd2));
check("★ 改配置带 id", upd.id === "m123");

// ── 5. 反解表单（编辑态）─────────────────────────────────────────────
const row = { id: "m1", card: "qwen", name: "Q", model: "qwen-max",
              base_url: "https://x/v1", window: 131072, has_api: true, api_tail: "abcd" };
const back = entryToForm(row);
check("★ 反解出名字/模型/地址", back.name === "Q" && back.model === "qwen-max");
check("★★ `api` **故意留空**（界面拿不到明文；空 = 不改）", back.api === "");
check("★ 有窗口 -> 展开高级选项 + 填回那个数",
      back.advanced === true && back.window === "131072");
check("★ 没窗口 -> 收着、且窗口格是空的",
      entryToForm({ ...row, window: null }).advanced === false &&
      entryToForm({ ...row, window: null }).window === "");

// ── 6. 显示 ───────────────────────────────────────────────────────────
check("凭据只给尾 4 位", apiHint(row) === "已保存 · 尾 abcd", apiHint(row));
check("没填过就是「未填」", apiHint({ has_api: false }) === "未填");
check("卡片副标题是 n / 上限", cardSubtitle({ configs: [1, 2] }, 10) === "2 / 10");
check("★ 满了就灰，且**说得出为什么**",
      canAdd({ configs: new Array(10).fill(0) }, 10).ok === false &&
      /先删一条/.test(canAdd({ configs: new Array(10).fill(0) }, 10).error));
check("没满 -> 可以建", canAdd({ configs: [1] }, 10).ok === true);

// ── 7. 联通测试的结论怎么说 ──────────────────────────────────────────
check("通了 -> 说得出它打的是哪个 URL",
      testVerdict({ ok: true, status: 200, tried: "GET x/models" }).ok === true);
check("★ 连不上（status 0）-> 说的是「连接失败」，不是「HTTP 0」",
      !testVerdict({ ok: false, status: 0, tried: "GET y", body: "连接失败：refused" }).ok &&
      /连接失败/.test(testVerdict({ ok: false, status: 0, tried: "GET y" }).text) &&
      !/HTTP 0\b/.test(testVerdict({ ok: false, status: 0, tried: "GET y" }).text));
check("★ 后端给了 message 就用它（文案只留一处）",
      testVerdict({ ok: false, status: 0, message: "已降级直连" }).text === "已降级直连");
check("普通 HTTP 错 -> 把状态码念出来",
      /HTTP 401/.test(testVerdict({ ok: false, status: 401, body: "bad key" }).text));

// ── 8. W189：能力覆盖口（三态）────────────────────────────────────
check("★ blankConfig 默认两格都是 auto（不勾的人行为一个字节不变）",
      blankConfig().capabilities.vision === "auto" &&
      blankConfig().capabilities.audio === "auto");
check("★ 只认那三个字符串",
      capModeVerdict("on").ok && capModeVerdict("off").ok && capModeVerdict("auto").ok,
      JSON.stringify(capModeVerdict("on")));
check("★★ 坏值被认出来（不是静默夹成 auto）", !capModeVerdict("yes").ok);
check("★ 大小写与空白能容忍（` ON ` -> on）", capModeVerdict(" ON ").value === "on");
check("★ 建配置默认带上 `auto/auto`",
      JSON.stringify(buildCreatePayload({ ...blankConfig("gpt"), model: "m" }, LIMITS)
        .capabilities) === JSON.stringify({ vision: "auto", audio: "auto" }), "");
const capped = { ...blankConfig("other"), model: "relay-model", advanced: true,
                 capabilities: { vision: "on", audio: "off" } };
check("★★ 勾了的两档真的进了请求体（端点少接一格 = 勾了等于没勾）",
      JSON.stringify(buildCreatePayload(capped, LIMITS).capabilities) ===
      JSON.stringify({ vision: "on", audio: "off" }), "");
check("★ 改配置那条也带",
      JSON.stringify(buildUpdatePayload("m_1", capped, LIMITS).capabilities) ===
      JSON.stringify({ vision: "on", audio: "off" }), "");
check("★ 高级区收着时**不拿看不见的坏值拦提交**",
      validateConfig({ ...blankConfig("gpt"), model: "m", advanced: false,
                       capabilities: { vision: "bogus" } }, LIMITS).ok);
check("★ 展开之后它就拦了",
      !validateConfig({ ...blankConfig("gpt"), model: "m", advanced: true,
                        capabilities: { vision: "bogus" } }, LIMITS).ok);
const e2 = entryToForm({ ...row, window: null,
                         capability_modes: { vision: "off", audio: "auto" } });
check("★★ 编辑时回填**用户选的那一档**（不是从行为值反推）",
      e2.capabilities.vision === "off" && e2.capabilities.audio === "auto",
      JSON.stringify(e2.capabilities));
check("★★ 有覆盖时**自动展开**高级区（否则改过的档看不见，下次保存会静默带回去）",
      e2.advanced === true);
check("★ 没覆盖、没窗口 -> 还是收着",
      entryToForm({ card: "gpt", name: "x", model: "m", window: null }).advanced === false);

console.log(`\n${failed.length === 0 ? "OK" : "FAILED: " + failed.join(", ")}`);
assert.equal(failed.length, 0);
