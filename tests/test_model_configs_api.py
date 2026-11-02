# -*- coding: utf-8 -*-
"""端配置页那六个端点 —— 经过 HTTP 层的读者。

    .venv/Scripts/python.exe tests/test_model_configs_api.py

## 为什么要有这一条（而不是只测 `core/model_configs.py`）

`core/model_configs.py` 那 91 条测的是判据；这一条测的是缝。
本仓第一条经 HTTP 的测试（`tests/test_allowlist_ui.py`）的 docstring 把理由
写死了：*「三处各自都"写对了"，合起来不工作，而没有任何东西会红」* ——
id 拼错、端点名差一个字母、布尔当字符串，都是那一类。

## 三条不变量（这一条专门钉它们）

1. 凭据绝不从读的那一侧出去 —— `GET /api/model-configs` 的响应体里
   一个 api 明文字节都不许有（只许 `has_api` + 尾 4 位）。
   这是"经 HTTP"才测得到的那半：判据层是对的，端点把 `entry` 原样 `jsonify`
   出去就全泄了。
2. 六个端点一律要 token —— 没带就 403（不是 200 空表）。
3. 「设为默认」不许谎报"当场生效" —— 它不热插拔正在跑的 provider
   （那会让跑着的几轮半新半旧），回执里必须说清要重启。

ASCII-only 输出（Windows 控制台默认 GBK）。
会写文件 -> 全程 `sandbox()`，首尾自带生产目录指纹机检。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from _sandbox import prod_diff, prod_snapshot, sandbox            # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


from core import model_configs as _MC
MC_CARDS = _MC.CARD_IDS


def _boot():
    import importlib
    import web.server as S
    importlib.reload(S)
    S._LAUNCH_TOKEN = "test-token-w113"
    S.app.config["TESTING"] = True
    from core import model_configs as MC
    MC.reset_for_tests()
    for p in (MC.LEDGER_PATH, MC.CREDS_PATH):
        try:
            p.unlink()
        except OSError:
            pass
    return S, S.app.test_client(), S._LAUNCH_TOKEN


def _get(c, path, token):
    return c.get(path + "?token=" + token)


def _post(c, path, token, body):
    return c.post(path + "?token=" + token, json=body)


SECRET = "sk-THIS-MUST-NEVER-COME-BACK-0123456789"


# ----------------------------------------------------------------------

def test_auth_and_shape():
    print("\n-- 1. 鉴权与形状 --")
    S, c, token = _boot()
    for path in ("/api/model-configs", "/api/model-configs/create",
                 "/api/model-configs/update", "/api/model-configs/delete",
                 "/api/model-configs/default", "/api/model-configs/test"):
        r = c.get(path) if path == "/api/model-configs" else c.post(path, json={})
        check("★ 没带 token 打 `%s` -> 403（不是 200 空表）" % path,
              r.status_code == 403, str(r.status_code))

    d = _get(c, "/api/model-configs", token).get_json() or {}
    cards = d.get("cards") or []
    check("★ 全部卡片都发出来（空卡也发 —— 「这张卡还没有配置」要看得见）",
          len(cards) == len(MC_CARDS), repr(len(cards)))
    check("★ 每张卡带 id / 名字 / provider 目录",
          all(k in cards[0] for k in ("id", "label", "provider", "configs")))
    check("★ 三条上限**由后端给**（前端不自己写死）",
          d.get("max_per_card") == 10 and d.get("max_name") == 40, json.dumps(
              {k: d.get(k) for k in ("max_per_card", "max_name")}))


def test_credentials_never_leak_over_http():
    print("\n-- 2. 凭据绝不从读的那一侧出去 --")
    S, c, token = _boot()
    r = _post(c, "/api/model-configs/create", token, {
        "card": "gpt", "name": "中转 A", "model": "gpt-4o",
        "base_url": "https://relay.example.com/v1", "api": SECRET})
    check("建一条 -> 200", r.status_code == 200, str(r.status_code))
    cid = ((r.get_json() or {}).get("entry") or {}).get("id")
    check("★ 建出来的那条有 id", bool(cid), repr(cid))

    body = _get(c, "/api/model-configs", token).get_data(as_text=True)
    check("★★ GET 的**响应体里没有 api 明文**（一个字节都不许有）",
          "THIS-MUST-NEVER" not in body and SECRET not in body, body[:300])
    d = json.loads(body)
    row = [r_ for card in d["cards"] for r_ in card["configs"]][0]
    check("★ 它带的是 `has_api` + 尾 4 位",
          row.get("has_api") is True and row.get("api_tail") == "6789", repr(row))
    #: 能力跟模型走 —— 端配置页换模型时，
    #:   能力不能再永远是"默认 True / 恒 False"。
    caps = row.get("capabilities") or {}
    check("★★ 能力跟模型走（`gpt-4o` 在表里能收图）",
          caps.get("vision") is True, repr(caps))
    check("★ 没声明音频 -> False", caps.get("audio") is False, repr(caps))
    check("★ 能力带**来源**（界面 title 要显示它是谁给的）",
          bool(caps.get("vision_source")) and bool(caps.get("audio_source")),
          repr(caps))
    check("★ **`create` 的回执里也没有明文**（新建那条最容易顺手回显）",
          "THIS-MUST-NEVER" not in r.get_data(as_text=True), r.get_data(as_text=True)[:300])

    #: update 不带 `api` -> 凭据一个字不动（判据层有，这里验端点有没有传错）
    r2 = _post(c, "/api/model-configs/update", token,
               {"id": cid, "name": "改个名"})
    check("★ `update` 只改名字 -> 200", r2.status_code == 200, str(r2.status_code))
    from core import model_configs as MC
    check("★★ `update` 缺席 `api` -> **凭据一个字不动**",
          MC.api_for(cid) == SECRET, repr(MC.api_for(cid)))
    check("★ `update` 的第二个参数 `base_url` 缺席 -> 它也一个字不动",
          MC.get(cid)["base_url"] == "https://relay.example.com/v1",
          repr(MC.get(cid)["base_url"]))


def test_capability_override_over_http():
    """三态能力覆盖口的端到端那一半。

    判据层（`test_model_configs.py::test_capability_override`）已经逐条钉过；
    这里只问一件事：接口有没有把它透传过去 —— 端点少接一格，
    界面上勾了等于没勾，而没有任何东西会红（同一口径只写一处）。
    """
    print("\n-- 2b. 能力覆盖口--")
    S, c, token = _boot()
    r = _post(c, "/api/model-configs/create", token, {
        "card": "other", "name": "中转 X", "model": "relay-model",
        "base_url": "https://relay.example.com/v1", "api": "sk-xxxx",
        "capabilities": {"vision": "on", "audio": "off"},
    })
    check("★ 建的时候带 `capabilities` -> 200", r.status_code == 200, str(r.status_code))
    cid = ((r.get_json() or {}).get("entry") or {}).get("id")
    d = _get(c, "/api/model-configs", token).get_json() or {}
    row = next((x for card in d["cards"] for x in card["configs"]
                if x.get("id") == cid), {})
    check("★★ 落盘的档被读回来（`capability_modes`：on/off）",
          row.get("capability_modes") == {"vision": "on", "audio": "off"},
          repr(row.get("capability_modes")))
    check("★★ 行为值也跟着走（`on` 压过表：查不到也开）",
          (row.get("capabilities") or {}).get("vision") is True
          and (row.get("capabilities") or {}).get("audio") is False,
          repr(row.get("capabilities")))
    check("★ 来源标明是**本配置**给的（不是 config.yaml）",
          (row.get("capabilities") or {}).get("vision_source") == "声明(本配置)",
          repr((row.get("capabilities") or {}).get("vision_source")))

    r = _post(c, "/api/model-configs/update", token,
              {"id": cid, "capabilities": {"vision": "auto"}})
    check("★ update 带 `capabilities` -> 200", r.status_code == 200, str(r.status_code))
    from core import model_configs as MC
    check("★★ 只给 `vision`：它改了，`audio` **一个字不动**",
          MC.get(cid)["capabilities"] == {"vision": "auto", "audio": "off"},
          repr(MC.get(cid)["capabilities"]))

    r = _post(c, "/api/model-configs/update", token,
              {"id": cid, "capabilities": {"vision": "maybe"}})
    check("★★ 坏值 -> **400**（不是静默夹取，也不是 500）",
          r.status_code == 400, str(r.status_code))
    check("★ 拒话点得出合法值", "auto / on / off" in (r.get_json() or {}).get("error", ""),
          repr((r.get_json() or {}).get("error")))
    check("★★ 被拒之后那一格**一个字节没动**",
          MC.get(cid)["capabilities"] == {"vision": "auto", "audio": "off"},
          repr(MC.get(cid)["capabilities"]))


def test_crud_and_default():
    print("\n-- 3. 增删改 + 设为默认 --")
    S, c, token = _boot()
    a = (_post(c, "/api/model-configs/create", token, {
        "card": "deepseek", "name": "A", "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com/v1", "api": "sk-aaaa"}).get_json()
        or {}).get("entry") or {}
    b = (_post(c, "/api/model-configs/create", token, {
        "card": "qwen", "name": "B", "model": "qwen-max",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api": "sk-bbbb"}).get_json() or {}).get("entry") or {}

    r = _post(c, "/api/model-configs/default", token, {"id": b["id"]})
    check("设为默认 -> 200", r.status_code == 200, str(r.status_code))
    check("★★ 回执**明说「要重启」**（不许谎报当场生效）",
          "重启" in (r.get_json() or {}).get("message", ""),
          (r.get_json() or {}).get("message", ""))

    r = _post(c, "/api/model-configs/delete", token, {"id": b["id"]})
    check("★★ 删默认那条 -> 400 + **带路线**（「先设另一个」）",
          r.status_code == 400 and "默认" in (r.get_json() or {}).get("error", ""),
          str(r.status_code) + " " + str((r.get_json() or {}).get("error")))

    #: 预检的读者：Claude 那张卡背后的 `anthropic` SDK 可能没装，
    #:   而这条端点若只写账本，界面回"已设为默认"、
    #:   模型一个字节没变，失败只躺在日志里（标签在说谎）。
    #:必须先试装、装得上才落盘（同 `skill_factory` 那条"先体检、后落盘"）。
    from core import model_configs as MC
    z = (_post(c, "/api/model-configs/create", token, {
        "card": "gpt", "name": "装不上的那条", "model": "gpt-4o",
        "base_url": "https://api.openai.com/v1", "api": "sk-z"}).get_json()
        or {}).get("entry") or {}
    default_before = MC.default_id()
    _real_preflight = MC.preflight
    MC.preflight = lambda cid: "模拟：这个 SDK 没装"
    try:
        r = _post(c, "/api/model-configs/default", token, {"id": z["id"]})
        check("★★ 装不上 -> **400 + 说清原因**（不许回一句成功、模型却一字没变）",
              r.status_code == 400 and "原因" in (r.get_json() or {}).get("error", ""),
              str(r.status_code) + " " + str((r.get_json() or {}).get("error")))
        check("★★ 而且**默认一个字都没改**（拒了就不许落盘）",
              MC.default_id() == default_before, MC.default_id())
    finally:
        MC.preflight = _real_preflight
    r = _post(c, "/api/model-configs/default", token, {"id": z["id"]})
    check("★ 装得上时照旧能设（上面那条不是把功能堵死了）",
          r.status_code == 200, str(r.status_code))

    r = _post(c, "/api/model-configs/delete", token, {"id": a["id"]})
    check("删非默认那条 -> 200", r.status_code == 200, str(r.status_code))
    r = _post(c, "/api/model-configs/update", token, {"id": "m_nope", "name": "x"})
    check("改不存在的 -> 404", r.status_code == 404, str(r.status_code))
    r = _post(c, "/api/model-configs/create", token, {"card": "根本没有这张卡",
                                                      "name": "x", "model": "y"})
    check("建在不存在的卡上 -> 400 且带路线",
          r.status_code == 400 and "卡片" in (r.get_json() or {}).get("error", ""),
          str(r.status_code))
    r = _post(c, "/api/model-configs/create", token, {"card": "gpt",
                                                      "name": "n" * 99, "model": "m"})
    check("名字超限 -> 400", r.status_code == 400, str(r.status_code))


def test_the_connectivity_probe():
    print("\n-- 4. 联通测试：真发一次（打一个**关着的**本机端口，不碰外网）--")
    S, c, token = _boot()
    e = (_post(c, "/api/model-configs/create", token, {
        "card": "other", "name": "本机测试", "model": "probe-model",
        "base_url": "http://127.0.0.1:9", "api": "sk-nope"}).get_json()
        or {}).get("entry") or {}
    r = _post(c, "/api/model-configs/test", token, {"id": e["id"]})
    check("★ 端点是通的（不是 500）", r.status_code == 200, str(r.status_code))
    d = r.get_json() or {}
    check("连不上时有结论：ok=false + status=0 + 一句人话",
          d.get("ok") is False and d.get("status") == 0, json.dumps(d)[:200])
    check("★★ 本机/内网地址**照样真发**（本地模型就在那儿），但要**说清**它在内网",
          d.get("scope") == "private" and bool(d.get("scope_note")),
          json.dumps({k: d.get(k) for k in ("scope", "scope_note")})[:200])
    check("★ 回执里**没有我们填的那把 key**",
          "sk-nope" not in r.get_data(as_text=True), r.get_data(as_text=True)[:200])

    r = _post(c, "/api/model-configs/test", token, {"id": "m_nope"})
    check("测不存在的 -> 404", r.status_code == 404, str(r.status_code))

    r = _post(c, "/api/model-configs/test", token,
              {"card": "other", "base_url": "ftp://x/y", "model": "m"})
    check("地址不是 http(s) -> 400", r.status_code == 400, str(r.status_code))

    #: 探针路径的读者：Anthropic 那条协议要自己补 `/v1`
    #:   （因为按约定地址不带 `/v1`），而 OpenAI 兼容那条的地址已经到 API 根
    #:直接接 `/models`。两条都拼 `/models`，Claude 卡永远 404，
    #:   而用户看到的是"上游回了 HTTP 404"（看着像对方的问题）。
    e2 = (_post(c, "/api/model-configs/create", token, {
        "card": "claude", "name": "Claude 中转", "model": "claude-sonnet-4",
        "base_url": "http://127.0.0.1:9", "api": "sk-anthropic"}).get_json()
        or {}).get("entry") or {}
    d2 = _post(c, "/api/model-configs/test", token, {"id": e2["id"]}).get_json() or {}
    check("★★ Claude 卡（Anthropic 协议）探针打的是 **`/v1/models`**",
          d2.get("tried", "").endswith("/v1/models"), repr(d2.get("tried")))
    check("★ 而 OpenAI 兼容那张打的还是 `/models`（别把两边写成一样的）",
          (d.get("tried") or "").endswith("/models")
          and not (d.get("tried") or "").endswith("/v1/models"), repr(d.get("tried")))
    check("★ Claude 卡的探针用的是 `x-api-key`（这条协议不是 Bearer）",
          d2.get("model") == "claude-sonnet-4", repr(d2.get("model")))

    #: Claude 卡地址留空时不许回"你没填地址" —— Anthropic 那侧 SDK 自带
    #:   官方默认（manifest 里那个空串是故意的）。
    e3 = (_post(c, "/api/model-configs/create", token, {
        "card": "claude", "name": "官方 Claude", "model": "claude-sonnet-4",
        "base_url": "", "api": "sk-x"}).get_json() or {}).get("entry") or {}
    d3 = _post(c, "/api/model-configs/test", token, {"id": e3["id"]}).get_json() or {}
    check("★★ Claude 卡留空地址 -> 用**官方默认**，不是回一句「你没填地址」",
          d3.get("base_url") == "https://api.anthropic.com", repr(d3.get("base_url")))


def test_the_old_switch_path_is_rescoped():
    print("\n-- 5. 老 WebUI 那个「设为当前」改道了 --")
    S, c, token = _boot()
    # 这里必须用桩：`_switch_provider` / `_switch_model` 会把
    #   生产 `config.yaml` 的 `model.provider/model_name/base_url/api_key`
    #   逐行改写。直接打真端点会悄悄改写那四行，而测试全绿
    #   （prod 指纹若不盖 `config.yaml`，这类写就照不出来）。
    #   这条要验的是"改道"那个判据，不是"config.yaml 怎么写"。
    calls: list = []
    real_provider, real_model = S._switch_provider, S._switch_model
    S._switch_provider = lambda p, m: calls.append(("provider", p, m))
    S._switch_model = lambda p, m: calls.append(("model_select", p, m))
    try:
        r = _post(c, "/api/config", token, {"section": "provider",
                                            "provider": "qwen",
                                            "model_name": "qwen-max"})
        check("★ **没有配置条目**时它照旧工作（老行为不拦）",
              r.status_code == 200, str(r.status_code))
        check("★ 而且**真的走到了**那个切换（不是被别的东西挡下来）",
              calls == [("provider", "qwen", "qwen-max")], repr(calls))

        _post(c, "/api/model-configs/create", token, {
            "card": "qwen", "name": "Q", "model": "qwen-max",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api": "sk-q"})
        r = _post(c, "/api/config", token, {"section": "provider",
                                            "provider": "openai",
                                            "model_name": "gpt-4o"})
        check("★★ **有配置条目之后**它改道：409 + 说清改去哪了"
              "（留着它就是「按了有回执、模型没变」的假按钮）",
              r.status_code == 409 and "端配置" in (r.get_json() or {}).get("error", ""),
              str(r.status_code) + " " + str((r.get_json() or {}).get("error")))
        check("★★ 而且**一个切换都没发生**（桩没被调到 —— 不是「改了但没人看」）",
              len(calls) == 1, repr(calls))
    finally:
        S._switch_provider, S._switch_model = real_provider, real_model


def test_session_model_endpoints():
    """`/api/session-model` —— 这一段用什么模型那两个口。

    这一条测的正是"经 HTTP 才看得见"的那半：
      · 归属核对（客户端传来的 id 不许给别的通道写一行）；
      · 先试装、后落盘（装不上，400 且账本一字不动）——
        判据层是对的，端点若先写再判就全废了。
    这条口要 `MemoryManager` 在位（它查 `short_term` 的归属），这里造一个
      真 `ShortTermMemory` 的替身（形状照 `tests/test_stars_api.py::_lab`）。
    """
    print("\n-- 6. `/api/session-model`：这一段用什么模型（批次 3）--")
    S, c, token = _boot()
    from core.session_key import session_key_for
    from core.memory.short_term import ShortTermMemory
    import bus.registry as R

    # 鉴权：两个口都要 token
    check("★ GET 没带 token -> 403", c.get("/api/session-model").status_code == 403, "")
    check("★ POST 没带 token -> 403",
          c.post("/api/session-model", json={}).status_code == 403, "")

    good = _post(c, "/api/model-configs/create", token,
                 {"card": "qwen", "name": "便宜的", "model": "qwen3.8-max",
                  "api": "k"}).get_json()["entry"]["id"]
    broken = _post(c, "/api/model-configs/create", token,
                   {"card": "gpt", "name": "坏了的那条", "model": "x",
                    "api": "k"}).get_json()["entry"]["id"]
    from core import model_configs as MC
    #: 它必须"存在但装不上" —— 这才是 `preflight` 那条路的入口。
    #:   从账本里删掉它是不对的，端点先判存在性，回的是 404
    #:   「没有这条配置」，而断言盯着 400，红。那是对的：两条路要分开测。
    #:   这里模拟"用户手改坏了"（模型名被清空 —— `create` 挡得住、手改挡不住）。
    MC._cache["configs"][broken]["model"] = ""

    st = ShortTermMemory("data/memory/short_term.db")
    key = session_key_for(S._web_session())
    st.init_session("sess_ok")
    st.open_thread(key, "sess_ok")

    class _M:
        short_term = st
    saved = R.get_memory_manager()
    R.register_memory_manager(_M())
    try:
        r = _get(c, "/api/session-model", token).get_json()
        check("★ GET 回一个列表 + 当前状态（没指定 storage_id 也不炸）",
              r.get("ok") and isinstance(r.get("options"), list), repr(r)[:160])

        r = _get(c, "/api/session-model&x=1".replace("&x=1", ""), token)
        r = c.get("/api/session-model?token=%s&storage_id=sess_ok" % token).get_json()
        check("★ GET 带上 storage_id：名字/模型名/卡片名**一起给**"
              "（只给 id 界面得自己再查一次 = 第二份真相源）",
              r.get("ok") and r.get("source") == "default"
              and any(o["id"] == good for o in r["options"]), repr(r)[:200])

        # 归属核对：不在名册里的 id
        r = _post(c, "/api/session-model", token,
                  {"storage_id": "sess_别的通道", "config_id": good})
        check("★★ 名册外的 storage_id -> 404（否则这条路能给**任意一段**写一行）",
              r.status_code == 404, "%s %s" % (r.status_code, r.get_json()))
        check("★ 而它**没有**写下任何一行",
              st.session_model("sess_别的通道") == "", "")

        # 先试装、后落盘
        r = _post(c, "/api/session-model", token,
                  {"storage_id": "sess_ok", "config_id": broken})
        check("★★ 装不上的那条 -> 400 且**带原因**",
              r.status_code == 400 and r.get_json().get("error"), repr(r.get_json())[:160])
        check("★★ 而账本**一字不动**（先试装后落盘 —— 免得说着切换成功、"
              "下一轮又退回原模型）", st.session_model("sess_ok") == "",
              repr(st.session_model("sess_ok")))

        r = _post(c, "/api/session-model", token,
                  {"storage_id": "sess_ok", "config_id": good})
        check("★ 装得上的那条 -> 200，且**真写进了库**",
              r.status_code == 200 and st.session_model("sess_ok") == good,
              repr((r.status_code, st.session_model("sess_ok"))))
        r = c.get("/api/session-model?token=%s&storage_id=sess_ok" % token).get_json()
        check("★★ 再读回来 `source == \"frozen\"`（界面据此说「改默认不影响它」）",
              r.get("source") == "frozen" and r.get("name") == "便宜的", repr(r)[:200])

        r = _post(c, "/api/session-model", token,
                  {"storage_id": "sess_ok", "config_id": ""})
        check("★ 空 config_id = 切回跟随默认（那一行被删掉）",
              r.status_code == 200 and st.session_model("sess_ok") == "",
              repr(st.session_model("sess_ok")))

        r = _post(c, "/api/session-model", token,
                  {"storage_id": "sess_ok", "config_id": "mdeadbeef"})
        check("★ 不存在的 id -> 404（不是 200 静默）", r.status_code == 404,
              repr(r.status_code))
    finally:
        R.register_memory_manager(saved)
        st.close()


# ----------------------------------------------------------------------

def main() -> int:
    print("=" * 62)
    print("Endpoint config page -- the six HTTP endpoints ")
    print("=" * 62)
    before = prod_snapshot()
    try:
        with sandbox():
            for fn in (test_auth_and_shape,
                       test_credentials_never_leak_over_http,
                       test_capability_override_over_http,
                       test_crud_and_default,
                       test_the_connectivity_probe,
                       test_the_old_switch_path_is_rescoped,
                       test_session_model_endpoints):
                try:
                    fn()
                except Exception as exc:                        # noqa: BLE001
                    check(fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        leaked = prod_diff(before, prod_snapshot())
        check("★★ 全程**生产 `data/` 一个字节都没动**",
              not leaked, repr(leaked)[:200])
    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
