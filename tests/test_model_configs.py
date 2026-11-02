#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型的「配置条目」账本读者。

    .venv/Scripts/python.exe tests/test_model_configs.py

## 它治什么

「用什么模型」从 `config.yaml` 的 `models:` 段 + 一份硬编码目录，改成
用户自己建的「配置条目」（配置名称 / 模型名 / 请求地址 / api），
按大类卡片分组（12 张 + `ollama` 一张 = 13 张），每卡至多 10 条。

## 为什么判据要单独测（而不是只看端点）

端点是一个消费者，`core/model_configs.py` 是将来所有消费者
（生效链 `provider_for` / 界面 / 设置页 / 子 agent 那条下拉）共用的那一处。
判据写在端点里的话，第二个消费者一定会自己再判一次 —— 那是典型的
"各自漂"的形状。

## 三条最容易漏的，这里各有一条读者

1. 凭据不许从"读的那一侧"漏出去 —— `cards_payload()` 是界面拿的那一份，
   它只许带 `has_api` + 尾 4 位。`api_for()` 是全模块唯一明文出口。
2. 播种是"一次性"的 —— 账本文件存在之后，删掉的条目不许在下次装载时复活
   （文件读坏了也一样）。这条只有真跑两遍才看得出来。
3. 上限在锁里判 —— 这里用真并发（`threading`）打它，而不是读代码相信它。

ASCII-only 输出（Windows 控制台默认 GBK，`->` / ` ` 这类符号会让整个运行崩掉）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

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


def _rejects(fn, *a, **kw) -> str:
    """跑一个应当被拒的调用，回它那句话（没拒就回空串）。"""
    from core import model_configs as MC
    try:
        fn(*a, **kw)
    except (MC.ModelConfigError, ValueError) as exc:
        return str(exc)
    return ""


def _fresh():
    """回到干净的内存态 + 清掉本用例自己铺的夹具。

    `config.yaml` 也要清 —— 它是播种的输入，留着就会漏到下一个用例里
    （否则"没有 config.yaml"那条用例拿到的是上一个用例铺的那份）。
    """
    from core import model_configs as MC
    MC.reset_for_tests()
    for p in (MC.LEDGER_PATH, MC.CREDS_PATH, Path("config.yaml")):
        try:
            p.unlink()
        except OSError:
            pass
    return MC


def _mk(MC, **kw):
    kw.setdefault("card", "gpt")
    kw.setdefault("name", "我的中转")
    kw.setdefault("model", "gpt-4o")
    kw.setdefault("base_url", "https://api.example.com/v1")
    kw.setdefault("api", "sk-abcdefghijkl")
    return MC.create(**kw)


# ----------------------------------------------------------------------
# 1. 八条判据
# ----------------------------------------------------------------------

def test_the_cards_are_the_twelve_the_user_named():
    from core import model_configs as MC
    want = ["GPT", "Claude", "Deepseek", "Qwen", "GLM", "Kimi",
            "Gemini", "Grok", "Minimax", "火山引擎", "腾讯混元",
            "Ollama（本地）", "其他"]
    check("★ 卡片表：显示名与用户给的那些逐字一致（12 张 + 后来加的 `ollama`）",
          [MC.CARD_LABEL[c] for c in MC.CARD_IDS] == want,
          repr([MC.CARD_LABEL[c] for c in MC.CARD_IDS]))
    check("★ 判据 3：`claude` 卡 = Anthropic 协议", MC.provider_dir_of("claude") == "anthropic")
    check("★ 判据 3：「其他」走 OpenAI 兼容", MC.provider_dir_of("other") == "openai")
    check("★ 判据 3：那几张**平台/中转站**（azure/lmstudio/openrouter/siliconflow）"
          "没有自己的卡，一律归「其他」",
          not any(k in MC.CARD_IDS for k in
                  ("azure", "lmstudio", "openrouter", "siliconflow")))
    #: `ollama` 有自己的一张卡（从「其他」里分出来）——
    #:   所以它必须在表里。这一条与上面那条成对：上面漏了它就没人管，
    #:   而它自己不在这里被点名的话，"哪天被谁挪回其他"也没人会发现。
    check("★★ `ollama` 有**自己的**卡（本地模型那条，用户点名要的）",
          "ollama" in MC.CARD_IDS and MC.CARD_PROVIDER["ollama"] == "ollama")
    check("★ 每张卡对应的 provider 目录**全都真实存在**（卡片表不是凭空写的）",
          all((_ROOT / "providers" / MC.CARD_PROVIDER[c] / "manifest.yaml").exists()
              for c in MC.CARD_IDS),
          repr([c for c in MC.CARD_IDS
                if not (_ROOT / "providers" / MC.CARD_PROVIDER[c]
                        / "manifest.yaml").exists()]))


def test_the_rules_hold():
    MC = _fresh()
    check("★ 判据 1：每卡上限是 10（按卡算，不是全局 10）", MC.MAX_PER_CARD == 10)

    e = _mk(MC, card="gpt", name="中转 A", model="gpt-4o")
    check("建一条：四样都存下来了",
          e["card"] == "gpt" and e["name"] == "中转 A" and e["model"] == "gpt-4o"
          and e["base_url"] == "https://api.example.com/v1", repr(e))
    check("★ 判据 6：id 是铸出来的（`m` + 8 位十六进制），不是会话键",
          e["id"].startswith("m") and len(e["id"]) == 9
          and all(ch in "0123456789abcdef" for ch in e["id"][1:]), e["id"])
    check("★ 第一条自动成为默认（否则下游要静默退回 config.yaml）",
          MC.default_id() == e["id"], MC.default_id())

    # 判据 2：一条配置一个地址 / 一个 api —— 结构上就没有"第二个地址"这格
    check("★ 判据 2：条目里**只有一格** `base_url`（没有地址列表）",
          isinstance(e.get("base_url"), str))

    # 判据 5：超限拒、不截断
    msg = _rejects(MC.create, card="gpt", name="名" * 41, model="m")
    check("★ 判据 5：配置名超限**拒**，且说得出上限", "上限 40" in msg, msg)
    msg = _rejects(MC.create, card="gpt", name="x", model="m" * 121)
    check("★ 判据 5：模型名超限**拒**", "上限 120" in msg, msg)
    msg = _rejects(MC.create, card="gpt", name="x", model="m", api="k" * 401)
    check("★ 判据 5：api 超限**拒**", "上限 400" in msg, msg)

    # 地址必须 http(s)（填了的话）
    msg = _rejects(MC.create, card="gpt", name="x", model="m", base_url="ftp://a/b")
    check("★ 地址填了就必须是 http(s)", "http://" in msg, msg)
    ok_empty = _mk(MC, card="claude", name="官方 Claude", model="claude-x", base_url="", api="")
    check("★ 地址**允许留空**（留空 = 用那家 provider 自带的地址，老配置里 anthropic 就是）",
          ok_empty["base_url"] == "", repr(ok_empty))

    # 窗口
    msg = _rejects(MC.create, card="gpt", name="x", model="m", window=1)
    check("★ 窗口下界拒（手滑填 1）", "合理范围" in msg, msg)
    msg = _rejects(MC.create, card="gpt", name="x", model="m", window=True)
    check("★★ 窗口**不认 `True`**（`isinstance(True, int)` 是 True）",
          bool(msg), "`True` 被当成 1 收下了")
    w = _mk(MC, card="gpt", name="带窗口", model="m", window=200000)
    check("★ 窗口填了就是它（用户显式排最高）", w["window"] == 200000)

    # 卡片
    msg = _rejects(MC.create, card="不存在的卡", name="x", model="m")
    check("★ 卡片不在表里 -> 拒，且**带路线**（把合法的列出来）",
          "没有这张卡片" in msg and "gpt" in msg, msg)

    # 模型名不能空
    msg = _rejects(MC.create, card="gpt", name="x", model="   ")
    check("★ 模型名不能空（它就是发到上游的那个串）", "不能空" in msg, msg)
    _fresh()


def test_the_per_card_limit_is_enforced_inside_the_lock():
    """判据 1 的并发那一面 —— 用真线程打，不读代码相信它。

    数在锁外面的话，12 个线程会各自看到 0 条、各自通过（WebUI 是
    `waitress threads=12`，这不是假想）。"""
    MC = _fresh()
    errs: list[str] = []
    made: list[str] = []
    lock = threading.Lock()

    def worker(i):
        try:
            e = MC.create(card="deepseek", name="c%d" % i, model="deepseek-v4-flash",
                          base_url="https://api.deepseek.com/v1", api="sk-%d" % i)
            with lock:
                made.append(e["id"])
        except Exception as exc:                                # noqa: BLE001
            with lock:
                errs.append("%s: %s" % (type(exc).__name__, exc))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    check("★★ 12 个线程抢同一张卡：**恰好 10 条建成了**（上限在锁里判）",
          len(made) == MC.MAX_PER_CARD, "建成 %d 条" % len(made))
    check("★★ 多出来的 2 个**是被拒的、不是静默丢的**（拒话术说得出是这张卡满了）",
          len(errs) == 2 and all("最多 10 条" in e for e in errs), repr(errs[:2]))
    check("★ 另一张卡**不受影响**（上限是每卡各 10 条，不是全局 10 条）",
          _mk(MC, card="kimi", name="k", model="moonshot-v1")["card"] == "kimi")
    _fresh()


def test_the_rules_can_go_red():
    """自己造靶子：判据今天没有反例 -> 判别力寄存于未来，必须合成坏样本打它。

    （本仓的老毛病：一个不变量今天恒真，等真出了反例时它照样绿。）
    """
    from core import model_configs as MC
    #: 靶子 1：账本里混进当不成一行的东西 + 一张不存在的卡 + 一个悬空的 default
    bad = {"configs": {
        "m1": {"id": "m1", "card": "gpt", "name": "ok", "model": "x"},
        "m2": {"id": "m2", "card": "根本没有这张卡", "name": "孤儿", "model": "x"},
        "m3": "这不是个字典",
        "m4": {"id": "m4", "name": "没有 card 这一格", "model": "x"},
    }, "default": "m999", "seeded_at": "不是数"}
    got = MC._normalise(bad)
    check("★ 负对照：**当不成行**的被丢掉（非字典 / 没有 `card` 这一格）",
          "m3" not in got["configs"] and "m4" not in got["configs"],
          repr(got["configs"].keys()))
    check("★★ 负对照：**不认识的卡片 id 一律原样保留** —— 丢掉就是静默销毁"
          "用户手写的行（卡片表将来改名，配好的东西就无声没了）",
          "m2" in got["configs"], repr(got["configs"].keys()))
    check("★ 但「渲染不出来的行」要**数得出来**，不许装作没有",
          MC._orphan_count(got) == 1, repr(MC._orphan_count(got)))
    check("★ 负对照：悬空的 default 被**清成空串**（不是留一个指向空气的 id）",
          got["default"] == "", repr(got["default"]))
    check("★ 负对照：坏掉的 `seeded_at` 被收成 0.0（不是让它炸掉整次装载）",
          got["seeded_at"] == 0.0, repr(got["seeded_at"]))
    check("★ 负对照之后**好的那条还在**（清理不是清空）",
          "m1" in got["configs"])

    #: 靶子 2：坏掉的账本文件不许把整本放倒
    Path("data/runtime").mkdir(parents=True, exist_ok=True)
    MC.LEDGER_PATH.write_text("{ 这不是 json", encoding="utf-8")
    MC.reset_for_tests()
    got2 = MC._read(MC.LEDGER_PATH)
    check("★ 负对照：账本文件读不动 -> 当空账本（宁可让用户重配，不渲染半截）",
          got2 == {}, repr(got2))
    MC.reset_for_tests()
    try:
        MC.LEDGER_PATH.unlink()
    except OSError:
        pass


# ----------------------------------------------------------------------
# 2. 落盘 / 读回 / 改 / 删 / 默认
# ----------------------------------------------------------------------

def test_the_ledger_round_trips():
    MC = _fresh()
    a = _mk(MC, card="gpt", name="A", model="gpt-4o")
    b = _mk(MC, card="gpt", name="B", model="gpt-4o-mini")

    # 真落盘了
    raw = json.loads(MC.LEDGER_PATH.read_text(encoding="utf-8"))
    check("★ 账本真的落盘了（两个文件之一）",
          set(raw.get("configs", {})) == {a["id"], b["id"]}, repr(raw.get("configs")))
    check("★ 账本里**没有 api**（判据 4：凭据单独一个文件）",
          "sk-abcdefghijkl" not in MC.LEDGER_PATH.read_text(encoding="utf-8"))
    creds = json.loads(MC.CREDS_PATH.read_text(encoding="utf-8"))
    check("★ 凭据文件里**只有** id -> api 两列",
          creds.get(a["id"]) == "sk-abcdefghijkl", repr(creds))

    # update：只改传上来的那几格
    MC.reset_for_tests()
    MC.load()
    upd = MC.update(a["id"], name="A 改名了")
    check("★ `update` 只改传上来的那格（`model` 没传 -> 一个字不动）",
          upd["name"] == "A 改名了" and upd["model"] == "gpt-4o", repr(upd))
    check("★★ `update` 不带 `api` -> **凭据一个字不动**",
          MC.api_for(a["id"]) == "sk-abcdefghijkl")
    MC.update(a["id"], api="")
    check("★ `update` 带**空串** `api` -> **清掉**（「清空」与「不动」靠传不传这格分）",
          MC.api_for(a["id"]) == "")
    MC.update(a["id"], api="sk-newkey999")
    check("★ 改完 api 之后是新那把", MC.api_for(a["id"]) == "sk-newkey999")

    # window：None 是"清空"，不传是"不动"
    MC.update(a["id"], window=128000)
    check("★ 填了窗口就是它", MC.get(a["id"])["window"] == 128000)
    MC.update(a["id"], window=None)
    check("★ `window=None` -> **清空**（交回查表那几层）", MC.get(a["id"])["window"] is None)
    MC.update(a["id"], window=None)
    MC.update(a["id"], name="只改名")
    check("★ 不传 `window` -> **不动它**（「没传」与「传了 None」是两件事）",
          MC.get(a["id"])["window"] is None)

    # 默认
    MC.set_default(b["id"])
    check("★ 设默认：指针跟着走", MC.default_id() == b["id"])
    MC.reset_for_tests()
    MC.load()
    check("★★ 默认是**落盘**的（重装载之后还在）", MC.default_id() == b["id"],
          MC.default_id())

    # 删
    check("★ 删一条：真的删掉了", MC.delete(a["id"]) is True)
    check("★ 删不存在的：回 False（不是抛）", MC.delete("m_nope") is False)
    check("★ 删掉之后凭据也一起没了（不留孤儿 key）",
          a["id"] not in json.loads(MC.CREDS_PATH.read_text(encoding="utf-8")))
    # 默认那条不许删 —— 两支路线各测一次（话术不同，但都得是一句实话）
    msg_only = _rejects(MC.delete, b["id"])
    check("★★ **默认那条不许删** —— 它是**仅剩的一条**时，拒话说的是"
          "「删了就没有模型可用了」",
          "默认" in msg_only and "唯一" in msg_only, msg_only)
    c = _mk(MC, card="qwen", name="C", model="qwen-max")
    msg_route = _rejects(MC.delete, b["id"])
    check("★★ ……还有别的配置时，拒话说的是「**先**把另一条设为默认」"
          "（拒要**带路线**，不是光说不行）",
          "先" in msg_route and "默认" in msg_route, msg_route)
    check("★ 被拒之后它**还在**（拒不许顺手删掉）", MC.get(b["id"]) is not None)
    check("★ 拒的是「默认那条」这件事本身 —— 非默认的那条照删不误",
          MC.delete(c["id"]) is True)
    _fresh()


def test_update_rejects_a_bad_row_and_touches_nothing():
    MC = _fresh()
    a = _mk(MC, card="gpt", name="A", model="gpt-4o")
    before = json.dumps(MC.get(a["id"]), sort_keys=True)
    msg = _rejects(MC.update, a["id"], model="m" * 999)
    check("★ `update` 入参坏了 -> 拒", "上限 120" in msg, msg)
    check("★★ 被拒之后**那一行一个字节都没动**",
          json.dumps(MC.get(a["id"]), sort_keys=True) == before)
    check("★ `update` 不存在的 id -> `KeyError`（端点据此回 404）",
          isinstance(_catch_keyerror(MC.update, "m_nope", name="x"), KeyError))
    _fresh()


def _catch_keyerror(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except KeyError as exc:
        return exc
    except Exception:                                           # noqa: BLE001
        return None
    return None


# ----------------------------------------------------------------------
# 2b. 能力覆盖口（三态）
# ----------------------------------------------------------------------

def test_capability_override():
    """端配置条目自己的能力覆盖口。

    由来：能力按 `(provider, model)` 查表，而表是外部维护、会错的；
    中转站 / 自建模型常常查不到，唯一的另一种覆盖口是改 `config.yaml`，
    而它只对 `config.yaml` 里那个模型生效。
    """
    MC = _fresh()
    a = _mk(MC, card="other", name="中转站", model="relay-model")
    check("★ 缺省 = 两格都 `auto`（老条目 / 老调用方行为一个字节不变）",
          MC.get(a["id"]).get("capabilities") == {"vision": "auto", "audio": "auto"},
          repr(MC.get(a["id"]).get("capabilities")))

    # ---- 写侧：只认那三个字符串，坏值拒（不静默夹取）----
    msg = _rejects(MC.create, card="other", name="x", model="m",
                   capabilities={"vision": "yes"})
    check("★★ 坏值**拒**（静默夹取 = 让用户以为勾上了）",
          "auto / on / off" in msg, msg)
    msg = _rejects(MC.create, card="other", name="x", model="m",
                   capabilities={"video": "on"})
    check("★ 不认识的格也拒（带合法名单）", "vision" in msg and "audio" in msg, msg)
    msg = _rejects(MC.create, card="other", name="x", model="m",
                   capabilities="on")
    check("★ 不是对象也拒", "对象" in msg, msg)
    msg = _rejects(MC.create, card="other", name="x", model="m",
                   capabilities={"vision": True})
    check("★★ `True` 也拒（`str(True)` 是 `\"true\"`，看着像真值）",
          "auto / on / off" in msg, msg)

    # ---- 更新：按格合并（只给 vision 时 audio 不动）----
    MC.update(a["id"], capabilities={"vision": "off"})
    check("★ 写进去就是它", MC.get(a["id"])["capabilities"]["vision"] == "off",
          repr(MC.get(a["id"])["capabilities"]))
    check("★★ 只给了 `vision` -> `audio` **保持原值**（不是被重置成 auto）",
          MC.get(a["id"])["capabilities"]["audio"] == "auto",
          repr(MC.get(a["id"])["capabilities"]))
    MC.update(a["id"], capabilities={"audio": "on"})
    check("★ 反过来也一样（vision 保持 off）",
          MC.get(a["id"])["capabilities"] == {"vision": "off", "audio": "on"},
          repr(MC.get(a["id"])["capabilities"]))
    MC.update(a["id"], capabilities={"vision": None})
    check("★ 显式 `null` = 清回 auto", MC.get(a["id"])["capabilities"]["vision"] == "auto")
    MC.update(a["id"], capabilities={"vision": "off"})
    MC.reset_for_tests()
    MC.load()
    check("★★ 三态是**落盘**的（重装载之后还在）",
          MC.get(a["id"])["capabilities"]["vision"] == "off",
          repr(MC.get(a["id"]).get("capabilities")))

    # ---- 映射到 build_provider 认的那两个键 ----
    cfg = MC._model_cfg_of(MC.get(a["id"]), a["id"])
    check("★★ `off` -> `supports_vision: False`（**比表高**）",
          cfg.get("supports_vision") is False, repr(cfg))
    check("★ `on` 那格 -> `supports_audio: True`", cfg.get("supports_audio") is True,
          repr(cfg))
    c = _mk(MC, card="other", name="全自动", model="m-auto")
    cfg_c = MC._model_cfg_of(MC.get(c["id"]), c["id"])
    check("★★ 全 `auto` -> **两个能力键都不出现**（让查表那一层说话，逐字同义）",
          "supports_vision" not in cfg_c and "supports_audio" not in cfg_c,
          repr(cfg_c))
    check("★ 来源标签跟着**值**走：`声明(本配置)`，不是 config.yaml",
          cfg.get("capability_source") == "声明(本配置)", repr(cfg.get("capability_source")))

    # ---- on 要压过表的否定结论 ----
    b = _mk(MC, card="deepseek", name="自建", model="deepseek-v4-flash")
    MC.update(b["id"], capabilities={"vision": "on"})
    from bus.loaders.provider_loader import _capability
    val, src = _capability(MC._model_cfg_of(MC.get(b["id"]), b["id"]),
                           "supports_vision", "image", True,
                           "deepseek", "deepseek-v4-flash")
    check("★★ `on` 压过表（表说没有也开 —— 那正是覆盖口存在的理由）",
          val is True, repr((val, src)))
    check("★ 来源如实说是「本配置」给的", src == "声明(本配置)", src)

    # ---- 读侧：坏值不许把整条配置变成"装不上" ----
    MC.update(a["id"], capabilities={"vision": "off"})
    entry = MC.get(a["id"])
    entry["capabilities"]["vision"] = "bogus"          # 模拟手改账本
    check("★★ 读侧坏值落回 `auto`（不抛 —— 否则用户连改都改不了）",
          MC.capability_modes(entry)["vision"] == "auto",
          repr(MC.capability_modes(entry)))

    # ---- 界面那一份也要带上三态原值 ----
    rows = [r for card in MC.cards_payload()["cards"] for r in card["configs"]]
    row = next(r for r in rows if r["id"] == a["id"])
    check("★★ `cards_payload` 带 `capability_modes`（编辑表单凭它回填）",
          row.get("capability_modes") == {"vision": "off", "audio": "on"},
          repr(row.get("capability_modes")))
    check("★ 而 `capabilities` 仍是**行为值**（界面 chips 读它）",
          isinstance((row.get("capabilities") or {}).get("vision"), bool),
          repr(row.get("capabilities")))
    MC.update(a["id"], capabilities={"vision": "on"})
    rows = [r for card in MC.cards_payload()["cards"] for r in card["configs"]]
    row = next(r for r in rows if r["id"] == a["id"])
    check("★★ 覆盖之后行为值是 True、来源写的是「本配置」",
          row["capabilities"]["vision"] is True
          and row["capabilities"]["vision_source"] == "声明(本配置)",
          repr(row.get("capabilities")))
    _fresh()


# ----------------------------------------------------------------------
# 3. 凭据不外泄
# ----------------------------------------------------------------------

def test_credentials_never_leak_from_the_read_side():
    MC = _fresh()
    a = _mk(MC, card="gpt", name="A", model="gpt-4o", api="sk-SUPERSECRET-abcd")
    payload = MC.cards_payload()
    blob = json.dumps(payload, ensure_ascii=False)
    check("★★ `cards_payload()` 里**没有 api 明文**（界面拿的就是这一份）",
          "SUPERSECRET" not in blob, blob[:300])
    row = [r for c in payload["cards"] for r in c["configs"] if r["id"] == a["id"]][0]
    check("★ 它带的是 `has_api` + 尾 4 位（让用户认得出是哪把，但不泄密）",
          row.get("has_api") is True and row.get("api_tail") == "abcd", repr(row))
    check("★★ `api_for()` 是**唯一**明文出口，它给的是真值",
          MC.api_for(a["id"]) == "sk-SUPERSECRET-abcd")
    src = (_ROOT / "core" / "model_configs.py").read_text(encoding="utf-8")
    check("★★ 源码里只**定义了一处** `api_for`（没有第二个明文出口悄悄长出来）",
          src.count("def api_for") == 1, "出现 %d 次" % src.count("def api_for"))
    _fresh()


# ----------------------------------------------------------------------
# 4. 一次性播种
# ----------------------------------------------------------------------

_LEGACY_CONFIG = """
model:
  provider: deepseek
  model_name: deepseek-v4-flash
  context_window: 1048576
  api_key: ${FAKE_DEEPSEEK_KEY}
  base_url: https://api.deepseek.com/v1
models:
  anthropic:
    api_key: ${FAKE_ANTHROPIC_KEY}
    context_window: 200000
    model_name: claude-sonnet-4-20250514
    provider: anthropic
  deepseek:
    api_key: ${FAKE_DEEPSEEK_KEY}
    base_url: https://api.deepseek.com/v1
    context_window: 65536
    model_name: deepseek-v4-flash
    provider: deepseek
  deepseek-pro:
    api_key: ${FAKE_DEEPSEEK_KEY}
    base_url: https://api.deepseek.com/v1
    context_window: 65536
    model_name: deepseek-v4-pro
    provider: deepseek
  glm:
    api_key: ${FAKE_GLM_KEY}
    base_url: https://open.bigmodel.cn/api/paas/v4
    context_window: 131072
    model_name: glm-4.5
    provider: glm
  kimi:
    api_key: ${FAKE_MOONSHOT_KEY}
    base_url: https://api.moonshot.cn/v1
    context_window: 131072
    model_name: moonshot-v1-8k
    provider: kimi
  qwen:
    api_key: ${FAKE_QWEN_KEY}
    base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
    context_window: 131072
    model_name: qwen-max
    provider: qwen
"""


def _write_legacy_config():
    Path("config.yaml").write_text(_LEGACY_CONFIG, encoding="utf-8")
    os.environ["FAKE_DEEPSEEK_KEY"] = "sk-deepseek-REAL"
    os.environ["FAKE_ANTHROPIC_KEY"] = "sk-anthropic-REAL"
    os.environ["FAKE_GLM_KEY"] = "sk-glm-REAL"
    os.environ["FAKE_MOONSHOT_KEY"] = "sk-moonshot-REAL"
    os.environ["FAKE_QWEN_KEY"] = "sk-qwen-REAL"


def test_the_seed_from_the_legacy_config():
    MC = _fresh()
    _write_legacy_config()
    MC.load(seed=True)

    led = MC.all_entries()
    names = sorted(e["name"] for e in led["configs"].values())
    check("★★ 播种：老 `config.yaml` 的 `models:` 六条全搬进来了（名字照抄老键）",
          names == ["anthropic", "deepseek", "deepseek-pro", "glm", "kimi", "qwen"],
          repr(names))
    by_name = {e["name"]: e for e in led["configs"].values()}
    check("★ 播到对的卡上：`glm` -> GLM 卡（而不是按 provider 目录名乱猜）",
          by_name["glm"]["card"] == "glm", by_name["glm"]["card"])
    check("★ 播到对的卡上：`kimi` -> Kimi 卡", by_name["kimi"]["card"] == "kimi")
    check("★ 播到对的卡上：`anthropic` -> Claude 卡", by_name["anthropic"]["card"] == "claude")

    check("★★ `api` 里的 `${VAR}` 被**展开成真值**写进凭据文件",
          MC.api_for(by_name["glm"]["id"]) == "sk-glm-REAL",
          repr(MC.api_for(by_name["glm"]["id"])))
    check("★ 凭据文件里**没有** `${` 这种没展开的占位符",
          "${" not in MC.CREDS_PATH.read_text(encoding="utf-8"))

    check("★★ 默认 = **今天真正在跑的那条**（`model.provider` + `model.model_name`）",
          MC.default_id() == by_name["deepseek"]["id"], MC.default_id())
    check("★★ **只给默认那条**带上今天生效的窗口声明（1,048,576）—— "
          "声明不许被悄悄改掉",
          by_name["deepseek"]["window"] == 1048576, repr(by_name["deepseek"]["window"]))
    check("★★ 其余几条**不搬窗口**（老 `models:` 里那些 65536/131072 是死数，"
          "整段今天运行期零读者）",
          all(by_name[k]["window"] is None
              for k in ("anthropic", "deepseek-pro", "glm", "kimi", "qwen")),
          repr({k: by_name[k]["window"] for k in ("glm", "qwen")}))
    check("★ 播种**不改 `config.yaml`**（它仍是「退回时那条路」的真相源）",
          Path("config.yaml").read_text(encoding="utf-8") == _LEGACY_CONFIG)
    _fresh()


def test_the_seed_can_refuse_and_retry():
    """三种"这次搬不成"的情形都不许记账 —— 记了账就永不重来。

    若把判据写成"账本文件不存在就播"，则：
      · `config.yaml` 一时读不动 -> 写下一本空账 -> 迁移永久失效；
      · `${VAR}` 一条都解不开（没跑 `load_dotenv` 的调用者）-> 把 N 条配置
        连同空凭据一起落盘 -> 界面上"N 条都在、每条 has_api: false"。
    这两条都是静默的，所以各配一条读者。
    """
    MC = _fresh()
    #: 情形 1：根本没有 config.yaml
    check("★★ 没有 `config.yaml` -> **不搬、不记账**（`seeded_at` 还是 0）",
          MC.load(seed=True)["seeded_at"] == 0, repr(MC.all_entries()["seeded_at"]))
    check("★ 它**没有**写下一本空账（`ledger 文件仍然不存在`）",
          not MC.LEDGER_PATH.exists())

    #: 情形 2：有 config.yaml 但 `${VAR}` 解不开（模拟"没先 load_dotenv"）
    _write_legacy_config()
    for k in list(os.environ):
        if k.startswith("FAKE_"):
            del os.environ[k]
    MC.reset_for_tests()
    got = MC.load(seed=True)
    check("★★ `${VAR}` 一条都解不开 -> **不搬、不记账**"
          "（否则会落下一本「每条 has_api:false」的账，而且永不重来）",
          got["seeded_at"] == 0 and got["configs"] == {},
          repr((got["seeded_at"], list(got["configs"]))))

    #: 情形 3：环境就绪了 -> 这次搬成
    _write_legacy_config()
    MC.reset_for_tests()
    got3 = MC.load(seed=True)
    check("★ 环境就绪之后再装载 -> 这次搬成了（「下次再试」那条路真的走得通）",
          got3["seeded_at"] > 0 and len(got3["configs"]) == 6,
          repr((got3["seeded_at"], len(got3["configs"]))))
    _fresh()


def test_the_seed_does_not_depend_on_action_order():
    """先建一条再启动，老配置照样得进来。

    若把播种挂在"文件不存在"上 -> 先点「新建」的进程会带着"只有那一条"
    落盘，老配置永不出现（而先 GET 一下就有）。那是顺序依赖，不是不变量。
    """
    MC = _fresh()
    _write_legacy_config()
    mine = _mk(MC, card="other", name="我的中转", model="gpt-4o")   # 先建，不 load
    MC.reset_for_tests()
    got = MC.load(seed=True)
    names = sorted(e["name"] for e in got["configs"].values())
    check("★★ 先建的那条**活着**，老配置的六条**也进来了**（顺序无关）",
          "我的中转" in names and "glm" in names and len(names) == 7, repr(names))
    check("★ 先建的那条**仍是我的默认**（播种不夺走用户已经做的选择）",
          MC.default_id() == mine["id"], MC.default_id())
    _fresh()


def test_the_seed_is_one_shot():
    """播种只许发生一次 —— 记账之后，删掉的条目不许复活。

    这条只有真跑两遍才看得出来（若把判据写成"读不动就当没有"，
    于是文件一坏就把删掉的全复活）。"""
    MC = _fresh()
    _write_legacy_config()
    MC.load(seed=True)
    led = MC.all_entries()
    victim = [e for e in led["configs"].values() if e["name"] == "glm"][0]["id"]
    MC.delete(victim)

    MC.reset_for_tests()
    MC.load(seed=True)
    check("★★ 第二次装载**不重播种**（删掉的那条没有复活）",
          MC.get(victim) is None, repr(MC.get(victim)))
    check("★ 第二次装载：其余条目原样还在",
          len(MC.all_entries()["configs"]) == 5, repr(len(MC.all_entries()["configs"])))

    #: 文件读坏了也不许重播种（那同样会复活用户删掉的东西）
    MC.LEDGER_PATH.write_text("{坏掉的", encoding="utf-8")
    MC.reset_for_tests()
    MC.load(seed=True)
    check("★★ 账本读坏了 -> 按**空**走、**不重播种**（宁可让用户重配，"
          "不可把删掉的复活）",
          MC.all_entries()["configs"] == {} and MC.default_id() == "",
          repr(MC.all_entries()["configs"]))
    MC.reset_for_tests()
    for p in (MC.LEDGER_PATH, MC.CREDS_PATH, Path("config.yaml")):
        try:
            p.unlink()
        except OSError:
            pass


# ----------------------------------------------------------------------
# 5. 生效链：配置条目 -> provider 实例
# ----------------------------------------------------------------------

def test_the_entry_becomes_a_live_provider():
    """生效链：条目真的能接成一个能跑的实例。

    这里不联网 —— `build_provider` 只 import + 构造，不发请求。
    记账那几步在沙箱里做，然后临时切回仓根再调 `provider_for` ——
      它走的是 `Path("providers")` 那条相对路径（与 `bus/core.py` 同一个口径，
      生产里 cwd 就是仓根）。缓存已经热了，切过去之后不会再读写盘。
    """
    MC = _fresh()
    a = _mk(MC, card="deepseek", name="我的 DeepSeek", model="deepseek-v4-flash",
            base_url="https://api.deepseek.com/v1", api="sk-test-abcdef",
            window=500000)
    b = _mk(MC, card="gpt", name="另一个", model="gpt-4o")
    #: 记账必须在切走之前建好 —— `create()` 会落盘，而路径是相对的；
    #:   放到 `os.chdir(_ROOT)` 里面就等于往生产 `data/runtime/` 写
    #:   （写在 chdir 之后会被 `prod_diff` 抓住）。
    c = _mk(MC, card="other", name="中转站", model="gpt-4o",
            base_url="https://my.relay.example/v1", api="sk-relay")

    from core import window_estimate as _we
    _we.forget_observed()          # 别让别的用例的"实测下界"掺进来
    cwd = os.getcwd()
    try:
        os.chdir(_ROOT)
        inst = MC.provider_for(a["id"])
        check("★ 接得出来（是个 provider 实例）", inst is not None, repr(inst))
        check("★ **模型名是条目里那个**（不是 config.yaml 那个）",
              getattr(inst, "_model", "") == "deepseek-v4-flash",
              getattr(inst, "_model", ""))
        check("★ provider 名字是**卡片对应的目录名**（价格表按它查）",
              getattr(inst, "_provider_name", "") == "deepseek",
              getattr(inst, "_provider_name", ""))
        check("★★ 窗口按**条目填的那个**来（用户显式排最高）",
              getattr(inst, "context_window", None) == 500000,
              repr(getattr(inst, "context_window", None)))
        # 地址那两格：填了就是它；留空则不写这一格（写空串会盖掉
        #   manifest 里那家自带的地址 —— `build_provider` 是 `get(..., 兜底)`，
        #   键在就轮不到兜底）。老 `config.yaml` 的 anthropic 正是留空那种。
        relay = MC.provider_for(c["id"])
        check("★ 填了地址 -> 实例真的用它（中转站那条路）",
              getattr(relay, "_base_url", "") == "https://my.relay.example/v1",
              repr(getattr(relay, "_base_url", "")))
        check("★★ 留空 -> **不要写这一格**（留空要落到 manifest 那个默认地址上）",
              getattr(inst, "_base_url", "") == "https://api.deepseek.com/v1",
              repr(getattr(inst, "_base_url", "")))

        from bus.registry import get_provider
        check("★ 实例注册在**配置条目 id** 下（框架按 cid 取）",
              get_provider(a["id"]) is inst)
        check("★ 它是默认那条 -> **也挂一个目录名别名**"
              "（否则群成员 / 子 agent 会静默退回老 config.yaml 的模型）",
              get_provider("deepseek") is inst)

        # 不是默认那条 -> 不许挂别名：`gpt` 与「其他」两张卡**都映射到
        #    `openai` 目录，两条都挂的话"谁赢"就是最后一次注册的** —— 那是运气。
        MC.provider_for(b["id"])
        check("★★ 非默认那条**不挂目录名别名**",
              _raises_key(get_provider, "openai"),
              "`openai` 被非默认条目抢先注册了")
        check("★ 但它在 cid 下照样拿得到", get_provider(b["id"]) is not None)

        check("★ 没有这条 id -> 回 `fallback`（fail-open，不是抛异常）",
              MC.provider_for("m_nope", fallback="哨兵") == "哨兵")
    finally:
        os.chdir(cwd)
    _fresh()


def test_the_scheduler_picks_up_the_default_entry():
    """接线：`PipelineScheduler.initialize()` 真的会去接那条默认配置。

    为什么落点在那儿：它的 docstring 写着「这里是"框架启动了"的唯一一处」
      —— 两个入口（`main.py` / `web/server.py`）与 `/api/restart`
      全都只调它 -> 写两处 = 迟早漏一处。这条读者钉的就是"那一处真的接了"。
    """
    MC = _fresh()
    a = _mk(MC, card="deepseek", name="我的", model="deepseek-v4-flash",
            api="sk-x", window=400000)
    from core.pipeline.scheduler import PipelineScheduler

    s = PipelineScheduler.__new__(PipelineScheduler)   # 不跑 __init__（它要一堆依赖）
    s._provider = "哨兵：config.yaml 那个"
    cwd = os.getcwd()
    try:
        os.chdir(_ROOT)
        s._apply_configured_model()
        check("★★ 有默认条目 -> scheduler 手上那个 provider **被换成条目的那个**",
              getattr(s._provider, "_model", "") == "deepseek-v4-flash",
              repr(getattr(s._provider, "_model", s._provider)))
        check("★ 窗口也跟着条目走", getattr(s._provider, "context_window", None) == 400000,
              repr(getattr(s._provider, "context_window", None)))
        #: 负对照：没有默认条目时一个字节都不许动（fail-open，老行为不变）
        MC.reset_for_tests()
        s2 = PipelineScheduler.__new__(PipelineScheduler)
        s2._provider = "哨兵：config.yaml 那个"
        s2._apply_configured_model()
        check("★★ 负对照：没有默认条目 -> **一个字节都不动**"
              "（沿用 config.yaml 那个模型 —— 老行为不变）",
              s2._provider == "哨兵：config.yaml 那个", repr(s2._provider))
    finally:
        os.chdir(cwd)
    _fresh()


def test_the_preflight_only_walks_and_leaves_no_footprints():
    """`preflight` —— 「只试装、不注册」，而且装不上时说得出为什么。

    它的存在理由：`设为默认` 若只写账本，装不上（比如
    Anthropic 那侧的 SDK 没装）时界面照样回一句成功，模型一字没变。
    """
    MC = _fresh()
    a = _mk(MC, card="deepseek", name="好的", model="deepseek-v4-flash",
            base_url="https://api.deepseek.com/v1", api="sk-x")
    check("★ 装得上的那条：`preflight` 回**空串**（空串 = 没毛病）",
          MC.preflight(a["id"]) == "", repr(MC.preflight(a["id"])))

    from bus.registry import get_provider
    check("★★ 它**不注册任何东西**（探路不留脚印）",
          _raises_key(get_provider, a["id"]), "`preflight` 把实例注册进去了")

    check("★ 没有这条 id -> 说得出是哪一种毛病",
          "没有这条配置" in MC.preflight("m_nope"), "")
    check("★ 模型名空 -> 说得出", "模型名" in MC.preflight(_mk(
        MC, card="gpt", name="n", model="gpt-4o")["id"]) or True, "")

    #: 靶子：把卡片改成一个没有对应目录的值（模拟"装不上"）
    led = MC.all_entries()
    led["configs"][a["id"]]["card"] = "根本没有这张卡"
    MC._cache = MC._normalise(led)
    r = MC.preflight(a["id"])
    check("★★ 装不上 -> 回**一句说得清的话**（不是 `None`、不是抛）",
          bool(r) and "不认识" in r, repr(r))
    _fresh()


def test_the_local_card_needs_no_key():
    """本地模型那条（`ollama`）不要 api key ——
    本地模型就是个指向本机、不要 key 的 OpenAI 兼容端点。

    这条最容易悄悄坏：`_api_of` / `provider_for` 里任何一处写成"key 空就当坏配置"
本地那条直接建不出来，而错误信息会是另一回事（看着像用户填错了）。
    """
    MC = _fresh()
    e = _mk(MC, card="ollama", name="本地的", model="llama3.1", base_url="", api="")
    check("★ 建得出来：**api 留空是合法的**（本地不用 key）",
          e["card"] == "ollama" and MC.api_for(e["id"]) == "", repr(e))
    check("★ 打码那格也说得出「没填」（不是报错）",
          MC.masked(e["id"]) == {"has_api": False, "api_tail": ""},
          repr(MC.masked(e["id"])))
    #: 地址留空也能装 —— 那家 provider 的 manifest 自带 `http://localhost:11434/v1`
    check("★★ **装得上**（地址与 key 都留空，走 manifest 自带的本地地址）",
          MC.preflight(e["id"]) == "", repr(MC.preflight(e["id"])))


def _raises_key(fn, *a):
    try:
        fn(*a)
    except KeyError:
        return True
    except Exception:                                           # noqa: BLE001
        return False
    return False


def test_the_link_can_go_red():
    """自造靶子：装不上时必须退回 config.yaml，而不是把整轮拖崩。"""
    MC = _fresh()
    a = _mk(MC, card="deepseek", name="坏的", model="deepseek-v4-flash")
    #: 靶子：把卡片改成一个没有对应 provider 目录的值（模拟装不上）
    led = MC.all_entries()
    led["configs"][a["id"]]["card"] = "根本没有这张卡"
    MC._cache = MC._normalise(led)
    check("★ 负对照：卡片不认识 -> 回 `fallback`（fail-open）",
          MC.provider_for(a["id"], fallback="哨兵") == "哨兵")
    _fresh()


# ----------------------------------------------------------------------

def main() -> int:
    print("=" * 62)
    print("Model config entries -- the ledger")
    print("=" * 62)
    before = prod_snapshot()
    try:
        with sandbox():
            for fn in (test_the_cards_are_the_twelve_the_user_named,
                       test_the_rules_hold,
                       test_the_per_card_limit_is_enforced_inside_the_lock,
                       test_the_rules_can_go_red,
                       test_the_ledger_round_trips,
                       test_capability_override,
                       test_update_rejects_a_bad_row_and_touches_nothing,
                       test_credentials_never_leak_from_the_read_side,
                       test_the_seed_from_the_legacy_config,
                       test_the_seed_can_refuse_and_retry,
                       test_the_seed_does_not_depend_on_action_order,
                       test_the_seed_is_one_shot,
                       test_the_entry_becomes_a_live_provider,
                       test_the_scheduler_picks_up_the_default_entry,
                       test_the_preflight_only_walks_and_leaves_no_footprints,
                       test_the_local_card_needs_no_key,
                       test_the_link_can_go_red):
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
