// 技能配置卡的表驱动判据（纯函数，node 可逐条断言）。
//
// 为什么单独一个模块：步 3 的判据是「加一个带配置的 skill
//     = 不发版」—— 那就要求界面不认具体技能，只认 manifest 里那份
//    `settings` schema。把"schema -> 字段表 / 条目表"这层抽成纯函数，
//    它才能被真跑（`tests/test_skill_config_ui.py`，同 `toasts.js` 的做法）。
//
// 与后端 `core/skill_settings.py` 同一套约定：
//    `x-secret` 的字段一律打码成星号串；界面原样送回 ≠ 清空（后端会把星号串
//    换回旧值）。这里不许自作聪明地把星号串变成空串。

/** 打码值 —— 与后端 `core/skill_settings.MASK` 逐字相同。 */
export const MASK = "****";

/** 一条 schema 的属性表 -> 要画的字段（顺序 = schema 里的书写顺序）。 */
export function fieldsOf(node) {
  const props = (node && typeof node === "object" && node.properties) || {};
  const out = [];
  for (const [name, spec] of Object.entries(props)) {
    if (!spec || typeof spec !== "object") continue;
    out.push({
      name,
      title: spec.title || name,
      type: spec.type || "string",
      format: spec.format || "",
      secret: spec["x-secret"] === true,
      options: Array.isArray(spec.enum) ? spec.enum.map(String) : [],
      description: spec.description || "",
      default: spec.default === undefined ? "" : spec.default,
    });
  }
  return out;
}

/** 顶层 schema -> 那个"条目表"字段（`entries` / `accounts`）的名字；没有 -> 空串。 */
export function entriesKeyOf(schema) {
  const props = (schema && schema.properties) || {};
  for (const [name, spec] of Object.entries(props)) {
    if (spec && typeof spec === "object" && spec.type === "object"
        && spec.additionalProperties) {
      return name;
    }
  }
  return "";
}

/** 条目表 -> 一张画得出来的字段表（`additionalProperties` 的那份）。 */
export function entryFieldsOf(schema) {
  const key = entriesKeyOf(schema);
  if (!key) return [];
  return fieldsOf(schema.properties[key].additionalProperties);
}

/** 读一份配置 -> 界面用的形状（不原地改入参）。 */
export function viewOf(cfg, schema) {
  const key = entriesKeyOf(schema) || "entries";
  const entries = (cfg && typeof cfg === "object" && cfg[key]
    && typeof cfg[key] === "object") ? cfg[key] : {};
  const out = {};
  for (const [name, entry] of Object.entries(entries)) {
    out[name] = (entry && typeof entry === "object") ? { ...entry } : {};
  }
  return {
    key,
    defaultName: String((cfg && cfg.default) || ""),
    entries: out,
  };
}

/** 一个空条目（每个字段给它的 `default`，没有就给空串）。 */
export function blankEntry(fields) {
  const out = {};
  for (const f of fields) {
    out[f.name] = f.default === undefined || f.default === null
      ? "" : f.default;
  }
  return out;
}

/** 界面那份 -> 交回后端的载荷（原样带着 ``，让后端换回旧值）。 */
export function payloadOf(view) {
  const out = { default: String(view.defaultName || "") };
  out[view.key] = view.entries || {};
  return out;
}

/** 一个值是不是"界面没动过、只是把打码值送回来"。
 *  判据只有一个 —— 与后端逐字相同的 ``。空串是真的空（用户清了）。 */
export function isMasked(value) {
  return value === MASK;
}
