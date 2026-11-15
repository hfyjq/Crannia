// 端配置页的两张查表：卡片的图标、以及请求地址该怎么填。
//
//   为什么单独一个文件（照 `group-form.js` / `endpoint-form.js` 那条）：两样都是表，
//   而表最会烂的方式是"加了卡片忘了加行"。纯函数 + node 逐条断言 +
//   `tests/test_desktop_endpoint.py` 那边按盘上的文件反查。
//
// 图标从哪来：
//    `web/static/img/provider-*.svg` 这 16 个是 LobeHub `@lobehub/icons` 的产物
//    （MIT，`Copyright (c) 2023 LobeHub`）—— 见仓根 `THIRD-PARTY.md`。
//    所以改这张表时，署名那一份也要跟着动（打包白名单里有它，
//      否则发出去的包里没有署名 = 等于没署）。
//
// 「其他」那张卡故意没有图标：它承接的是 `azure` / `ollama` / `lmstudio` /
//    `openrouter` / `siliconflow` 这些平台与中转站，拿任何一家的 logo 去代表它
//    都是指错人（用户会以为自己配的是 OpenAI）。用下面那个自绘的兜底字形。

/** 卡片 id -> `web/static/img/` 下的文件名。
 *
 * 键是卡片 id（不是 provider 目录名）—— 界面只知道卡片。
 * `glm` -> `provider-zhipu.svg`、`kimi` -> `provider-moonshot.svg`：
 *   卡片名是用户认得的产品名，文件名跟着厂商（`providers/` 目录也是这么命名的）。
 */
export const PROVIDER_ICON = {
  gpt: "provider-openai.svg",
  claude: "provider-anthropic.svg",
  deepseek: "provider-deepseek.svg",
  qwen: "provider-qwen.svg",
  glm: "provider-zhipu.svg",
  kimi: "provider-moonshot.svg",
  gemini: "provider-gemini.svg",
  grok: "provider-grok.svg",
  minimax: "provider-minimax.svg",
  volcengine: "provider-volcengine.svg",
  hunyuan: "provider-hunyuan.svg",
  ollama: "provider-ollama.svg",
  other: "",                       // ← 有意留空，见文件头
};

/** 卡片 -> 图标地址（必须走 `client.asset`）。
 *
 * 回 `""` = 没有图标，用兜底字形（不是报错、也不是拿别人的 logo 顶上）。
 *
 * 这里不许有"裸路径"的兜底分支：`/static/…` 在 `file://` 下会变成
 *    盘根目录（裂图）—— `tests/test_desktop_assets.py` 就是钉这件事的，
 *    而它认的判据是"那个字面量必须是 `.asset(` 的直接实参"。
 *，拿不到 `client` 时宁可没有图标，也不回一个注定裂掉的地址
 *    （不要写 `:"/static/img/" + f` 这种兜底）。
 */
export function iconUrl(client, cardId) {
  const f = PROVIDER_ICON[cardId];
  if (!f) return "";
  if (!client || typeof client.asset !== "function") return "";
  return client.asset("/static/img/" + f);
}

/** 每一张卡的两格提示：模型名的范例 + 请求地址的约定。
 *
 * `model` 那几项是范例（`gpt-6.1-sol` / `claude-haiku-5-5` /
 *   `gemini-3.8-flash` / `minimax-m2.5` / `grok-4.6` / `glm-5.3-flash` / `kimi-k2.6` /
 *   `qwen3.8-max` / `deepseek-flash`）；`volcengine` / `hunyuan` / `other`
 *   按同一风格补齐。
 *   它们只是占位符（写在水印里），不是"我们内置了这些模型" ——
 *     用户填什么都行，这一格从不校验。
 */
export const MODEL_EXAMPLE = {
  gpt: "gpt-6.1-sol",
  claude: "claude-haiku-5-5",
  //:用户报：「这个示例有人反应不对」——
  //:上游只认 `deepseek-flash` / `deepseek-v4-pro`，而水印写着
  //:`deepseek-v4.1-flash`（一个不存在的名字），照抄的人直接吃 400。
  //:范例必须是上游真的认的名字，不是"看起来像"的名字。
  deepseek: "deepseek-flash",
  qwen: "qwen3.8-max",
  glm: "glm-5.3-flash",
  kimi: "kimi-k2.6",
  gemini: "gemini-3.8-flash",
  grok: "grok-4.6",
  minimax: "minimax-m2.5",
  volcengine: "doubao-seed-2-1-pro-260628",   // 范例
  hunyuan: "hunyuan-turbo",           // 范例
  //:本地那条：填你 `ollama list` 里真有的那个名字（这里给的是
  //: `providers/ollama/manifest.yaml` 的 `default_model`，与仓库数据一致）。
  ollama: "llama3.1",
  other: "gpt-4o",                   // 范例（中转站，填上游认的名字）
};

/** api 那一格的提示 —— 所有卡片共用一条（它讲的是同一件事）。
 *
 * 不要在水印里写具体 key 格式（如 `sk-…`） ——
 *    那是个假设，而它只在 OpenAI / DeepSeek 那一族成立：
 *    Anthropic 是 `sk-ant-`、Gemini 是 `AIza…`、智谱是 `<id>.<secret>`、
 *    火山是另一套、而中转站给什么就是什么。
 *，拿一个格式当水印，只会让格式不一样的人以为自己的 key 填错了。
 *    这一格写中性的「只需要填这里，下方配置会自动填充」就够。
 *
 * 后端从不校验 key 的形状（`core/model_configs._api_of` 只看长度）——
 *    所以这一条纯是界面文案，改它不动任何判据。
 */
/** 请求地址那一格的提示 —— 跟着卡片变。
 *
 * 判据是这张卡背后是哪套协议（`providers/<目录>/manifest.yaml` 的 `api_mode`），
 *   不是"OpenAI 一律加 /v1"：
 *     · `claude` 卡 = Anthropic 原生，SDK 自己补 `/v1/messages`
 *，地址到 host 为止、不要以 `/v1` 结尾。
 *     · 其余 11 张 = OpenAI 兼容，补 `/chat/completions`
 *，地址要到那一层 API 根为止。那个根不一定是 `/v1`：
 *       `glm` 是 `/api/paas/v4`、`gemini` 是 `/v1beta/openai`、`volcengine` 是 `/api/v3`。
 *，所以 `example` 给的是那一家的真地址（抄它最省事），
 *         而提示说的是"到 API 根为止，不要再往下补端点"。
 *
 * 只提示、不代改：自动增删 `/v1` 会把本来就对的自定义路径改坏
 *   （本地网关 `http://127.0.0.1:8080/v1` 就是反例 —— 那个 `/v1` 是人家的路径）。
 */
export const ADDRESS_HINT = {
  claude: {
    example: "https://api.deepseek.com/anthropic",
    note: "Anthropic 兼容，不要以 / 结尾",
  },
  gpt: { example: "https://api.openai.com/v1",
         note: "OpenAI 兼容，以 /v1 结尾" },
  deepseek: { example: "https://api.deepseek.com/v1",
              note: "OpenAI 兼容，以 /v1 结尾" },
  qwen: { example: "https://dashscope.aliyuncs.com/compatible-mode/v1",
          note: "OpenAI 兼容，以 /v1 结尾" },
  glm: { example: "https://open.bigmodel.cn/api/paas/v4",
         note: "OpenAI 兼容，以 /api/paas/v4 结尾" },
  kimi: { example: "https://api.moonshot.cn/v1",
          note: "OpenAI 兼容，以 /v1 结尾" },
  gemini: { example: "https://generativelanguage.googleapis.com/v1beta/openai",
            note: "OpenAI 兼容，以 /v1beta/openai 结尾" },
  grok: { example: "https://api.x.ai/v1",
          note: "OpenAI 兼容，以 /v1 结尾" },
  minimax: { example: "https://api.minimax.chat/v1",
             note: "OpenAI 兼容，以 /v1 结尾" },
  volcengine: { example: "https://ark.cn-beijing.volces.com/api/v3",
                note: "OpenAI 兼容，以 /api/v3 结尾" },
  hunyuan: { example: "https://api.hunyuan.cloud.tencent.com/v1",
             note: "OpenAI 兼容，以 /v1 结尾" },
  //:本地这条：本地模型不是新机制，
  //:就是个指向本机、不要 key 的 OpenAI 兼容端点。
  ollama: {
    example: "http://localhost:11434/v1",
    note: "本地模型，以 /v1 结尾",
  },
  other: {
    example: "https://你的中转站/v1",
    note: "OpenAI 兼容，以 /v1 结尾",
  },
};

/** 取一张卡的提示：`{model, example, note}`（`model` = 模型名那格的范例）。
 *
 * 认不出的卡片 -> 走「其他」那一份（兜底不空手）。
 */
export function addressHint(cardId) {
  const h = ADDRESS_HINT[cardId] || ADDRESS_HINT.other;
  //:`model` 与 `ADDRESS_HINT` 分两张表（那张表的名字只讲地址），
  //:但调用方只想问一次，在这里合起来交出去（一处定义，不是两处各拼一次）。
  return {
    model: MODEL_EXAMPLE[cardId] || MODEL_EXAMPLE.other,
    example: h.example,
    note: h.note,
  };
}
