// 端配置页那两张查表（图标 + 地址提示）的纯函数读者。
//
//   跑法： node packages/ui/tests/provider-icons.test.mjs
//
// 这里钉的是表本身（有没有漏行、有没有指错人）。
//   "图标文件到底在不在盘上"这一类这一份查不了（node 这边没有那批文件的知识）
//   所以它在 `tests/test_desktop_endpoint.py` 里按真目录反查。两边分工，别重复也别都缺。

import assert from "node:assert/strict";
import { ADDRESS_HINT, addressHint, API_HINT, iconUrl, MODEL_EXAMPLE, PROVIDER_ICON }
  from "../src/provider-icons.js";

let failed = [];
function check(label, ok, note = "") {
  if (ok) {
    console.log(`  [ok]   ${label}`);
  } else {
    failed.push(label);
    console.log(`  [FAIL] ${label}${note ? "  -- " + note : ""}`);
  }
}

 //:这 13 个必须与后端 `core/model_configs.py::CARD_IDS` 一字不差
//:   （两份名单各写各的 = 迟早漂；`tests/test_desktop_endpoint.py` 那边会跟后端对账）。
const CARDS = ["gpt", "claude", "deepseek", "qwen", "glm", "kimi",
               "gemini", "grok", "minimax", "volcengine", "hunyuan",
               "ollama", "other"];

// ── 1. 图标表 ────────────────────────────────────────────────────────
check("★ 13 张卡**一个不漏**地在图标表里（漏一行 = 那张卡的图标是 undefined）",
      CARDS.every((c) => c in PROVIDER_ICON), CARDS.filter((c) => !(c in PROVIDER_ICON)).join(","));
check("★ 表里没有多出来的卡（多了 = 删卡片时忘了收）",
      Object.keys(PROVIDER_ICON).every((c) => CARDS.includes(c)));
check("★★ 「其他」**故意没有厂商图标** —— 拿任何一家的 logo 去代表它都是指错人",
      PROVIDER_ICON.other === "");
check("★ 其余 11 张**都有**", CARDS.filter((c) => c !== "other")
      .every((c) => PROVIDER_ICON[c] && PROVIDER_ICON[c].endsWith(".svg")));
check("★ `glm` -> zhipu、`kimi` -> moonshot（卡片名是产品名，文件名跟厂商）",
      PROVIDER_ICON.glm === "provider-zhipu.svg" && PROVIDER_ICON.kimi === "provider-moonshot.svg");
check("★ 文件名**没有重号**（两张卡指到同一个文件 = 有一张指错了）",
      new Set(CARDS.map((c) => PROVIDER_ICON[c]).filter(Boolean)).size === 12);

const fakeClient = { asset: (p) => "http://x" + p };
check("★ `iconUrl` 走 `client.asset`（打包后 base 会变）",
      iconUrl(fakeClient, "gpt") === "http://x/static/img/provider-openai.svg",
      iconUrl(fakeClient, "gpt"));
check("★ 没有图标那张回**空串**（调用方据此走兜底字形，**不是**报错）",
      iconUrl(fakeClient, "other") === "");

//:拿不到 `client` 时宁可没有图标，也不许回一个裸的 `/static/…`
//:   —— 那个在 `file://` 下是盘根目录（裂图），而 `tests/test_desktop_assets.py`
//:   正是钉这件事的（它认"必须是 `.asset(` 的直接实参"）。
//:第一版这里写了个兜底分支，当场被那个读者抓红。
check("★★ 没有 `client` -> 回**空串**（宁可没图标，也不给一个注定裂掉的地址）",
      iconUrl(undefined, "gpt") === "" && iconUrl({}, "gpt") === "");

// ── 2. 地址提示表 ────────────────────────────────────────────────────
check("★ 13 张卡**一个不漏**地有提示", CARDS.every((c) => ADDRESS_HINT[c]));
check("★ 每一条都**既有范例又有话**（只给范例 = 没解释；只给话 = 用户还得猜）",
      CARDS.every((c) => ADDRESS_HINT[c].example && ADDRESS_HINT[c].note));

check("★★ Claude 卡说的是「**不要**以 /v1 结尾」（SDK 自己补 /v1/messages）",
      /不要以\s*\/v1\s*结尾/.test(ADDRESS_HINT.claude.note), ADDRESS_HINT.claude.note);
check("★★ 而 Claude 卡的范例**真的不带 /v1**（话与范例不许互相打架）",
      !/\/v1$/.test(ADDRESS_HINT.claude.example), ADDRESS_HINT.claude.example);
check("★★ 「其他」（中转站）说的是「**要带 /v1**」—— 与 Claude 卡**正好相反**",
      /要带\s*\/v1/.test(ADDRESS_HINT.other.note) && /\/v1$/.test(ADDRESS_HINT.other.example),
      ADDRESS_HINT.other.note);

check("★★ 至少两条提示在提醒「这一家的根不是 /v1」（`glm` 与 `volcengine`）——"
      + "一刀切说 /v1 会把这两家指错",
      /不是\s*\/v1/.test(ADDRESS_HINT.glm.note) && /不是\s*\/v1/.test(ADDRESS_HINT.volcengine.note));
check("★ 认不出的卡片 -> 兜底**不空手**（回「其他」那一份）",
      addressHint("根本没有这张卡").example === ADDRESS_HINT.other.example);

check("★★ 提示里**不许出现 markdown 的 `**`**（它是当文本渲染的，会原样显示星号）",
      CARDS.every((c) => !ADDRESS_HINT[c].note.includes("**")),
      CARDS.filter((c) => ADDRESS_HINT[c].note.includes("**")).join(","));


// ── 3. 模型名那格的范例 ─────────────────────────────────────────────
check("★ 13 张卡**一个不漏**地有模型名范例", CARDS.every((c) => MODEL_EXAMPLE[c]));
check("★ 范例都是**非空的模型串**（不是一句中文说明 —— 它是水印，要像名字）",
      CARDS.every((c) => MODEL_EXAMPLE[c] && !/[一-龥]/.test(MODEL_EXAMPLE[c])),
      CARDS.filter((c) => /[一-龥]/.test(MODEL_EXAMPLE[c] || "")).join(","));
//:这 9 个是用户逐个给的 —— 逐字钉住，免得以后被谁"顺手改得更好"。
//:   （另外 3 个 volcengine / hunyuan / other 是按同一风格补的，不在这一组里。）
//: deepseek 那一格被用户当场改掉：上游只认 `deepseek-flash`，
//:而 `deepseek-v4.1-flash` 不存在 —— 照抄水印的人直接吃 400。
//:，这条断言跟着改（它钉的是"范例必须是上游真认的名字"，不是某个字符串）。
const GIVEN = {
  gpt: "gpt-6.1-sol", claude: "claude-haiku-5-5", deepseek: "deepseek-flash",
  qwen: "qwen3.8-max", glm: "glm-5.3-flash", kimi: "kimi-k2.6",
  gemini: "gemini-3.8-flash", grok: "grok-4.6", minimax: "minimax-m2.5",
};
for (const [card, want] of Object.entries(GIVEN)) {
  check("★★ 用户给的范例逐字没变：" + card + " -> " + want,
        MODEL_EXAMPLE[card] === want, MODEL_EXAMPLE[card]);
}
check("★ `addressHint()` 把模型名范例也带出来了（调用方只问一次）",
      addressHint("grok").model === "grok-4.6"
      && addressHint("没这张卡").model === MODEL_EXAMPLE.other);

console.log(`\n${failed.length === 0 ? "OK" : "FAILED: " + failed.join(", ")}`);

// ── 4. api 那格的提示 ───────────────────────────────────────────────
//:用户当场指出的：那一格原来写着 `sk-…`，而它只是个假设 ——
//:   各家和各中转站格式都不一样（有的 sk-、有的 AIza、有的 <id>.<secret>）。
//:拿一个格式当水印，只会让格式不一样的人以为自己填错了。
check("★★ api 的提示**不许把某一种前缀说成通用**（`sk-` 只对 OpenAI 那一族成立）",
      !API_HINT.includes("sk-…") && !API_HINT.includes("必须"), API_HINT);
check("★ 它要说清「格式各家不一样、照上游给的整串贴」",
      /各家|不一样/.test(API_HINT) && /整串/.test(API_HINT), API_HINT);
check("★ 它是**一条共用的**（不按卡片分 —— 这件事对所有卡片是同一件）",
      typeof API_HINT === "string" && API_HINT.length > 20);
assert.equal(failed.length, 0);
