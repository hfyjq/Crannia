# -*- coding: utf-8 -*-
"""设置包的导出 / 导入的机械读者。

## 钉住什么

1. 本地项与派生值永不进包：`NEVER_RESTORE` 那六格、`LOCAL_ONLY` 那四格
   路径、`DERIVED_KEYS` 那 22 个 GET 派生值。
2. 包是"基准 ∪ 当前"：`settings.json` 只装用户改过的键（新装甚至没有这个文件），
   只导出它等于导出一本半截设置。
3. 凭据只有勾了才进包，且是密文：包里搜不到哨兵串；口令对了能解回来；
   口令错了拒（不是"回一个空字典"）。
4. 两段式：`dry_run` 一个字节都不写 —— 端点那一层也一样（不传就是只计划）。
5. 机器相关项要显式勾：默认一项都不应用（`boot_on_startup` 会改注册表）。
6. 外来的包不许写 `NEVER_RESTORE`：包里硬塞 `web_token` / `workspace` 也不认。
7. 账本只并、不删：导入别人的包不会把你本机的配置条目抹掉。
8. `apply_import` 不写 `settings.json`（那一半归端点，它还要跑收尾）。

用法::

    .venv/Scripts/python.exe tests/test_settings_transfer.py

ASCII-only 输出（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from _sandbox import sandbox                                            # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []

SENTINEL = "SENTINEL-SECRET-9f3a"


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


def _reset_model_configs() -> None:
    """账本模块有进程级缓存 —— 换沙箱（= 换账本文件）时必须清掉。"""
    from core import model_configs as MC
    MC._cache = None
    MC._creds = None


def _write(path: str, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _minimal_bundle(**over) -> dict:
    b = {"kind": "crannia-settings", "schema": 1, "app_version": "test",
         "exported_at": "2026-10-27T00:00:00", "settings": {},
         "allowlist": {"egress": [], "command": [], "email_to": [],
                       "computer_app": []},
         "model_configs": {"configs": {}, "default": ""},
         "gui": {}, "credentials": False}
    b.update(over)
    return b


# 1 / 2. 包里有什么

def test_the_bundle_never_carries_local_or_derived_keys():
    """本地项 / 派生值 / 凭据键一个都不许出现在 `settings` 段。"""
    from core import settings_transfer as ST
    from core.settings_baseline import NEVER_RESTORE

    cur = {
        "web_token": "TOKEN", "skill_configs": {"mail_parser": {}},
        "workspace": "D:/mine", "work_groups": ["g"], "watchers": [{"id": 1}],
        "forbidden_directories": ["D:/no"],
        "mode": "manual", "effort_levels": [], "leaf_md": {"chars": 3},
        "stream_output": True,
    }
    got = ST.exportable_settings(cur)
    bad = sorted(k for k in got if k in NEVER_RESTORE or k in ST.DERIVED_KEYS)
    check("★★ 本地项 / 凭据 / 派生值一个都没进包", not bad, repr(bad))
    check("★ 普通偏好进包了（判据不是空跑）", got.get("stream_output") is True,
          repr(got.get("stream_output")))
    check("★ 四条路径一格都不在包里",
          not ({"workspace", "work_groups", "watchers",
                "forbidden_directories"} & set(got)), repr(sorted(got)))


def test_the_bundle_fills_in_the_factory_baseline():
    """「基准 ∪ 当前」—— 当前只改了一格，包里也要有整份快照。"""
    from core import settings_transfer as ST
    got = ST.exportable_settings({"stream_output": True})
    check("★ 当前值在", got.get("stream_output") is True)
    check("★ 基准里那些「没改过」的键也在（memory_write / cascade_enabled）",
          got.get("memory_write") is True and got.get("cascade_enabled") is True,
          "%r / %r" % (got.get("memory_write"), got.get("cascade_enabled")))
    check("★ `_comment` 不是设置", "_comment" not in got)


# 3 / 4. 凭据

def test_credentials_are_encrypted_and_round_trip():
    from core import settings_transfer as ST

    _write(".env", "DEEPSEEK_API_KEY=%s\n" % SENTINEL)
    _write("data/api_keys.json", json.dumps({"k": SENTINEL}))
    payload = ST.collect_credentials({"web_token": SENTINEL,
                                      "skill_configs": {"github": {"token": SENTINEL}}})
    check("★ 收到的凭据里有 .env / api_keys / web_token / skill_configs",
          {"env", "api_keys", "web_token", "skill_configs"} <= set(payload),
          repr(sorted(payload)))
    blob = ST.encrypt(payload, "pa55word")
    back = ST.decrypt(blob, "pa55word")
    check("★★ 口令对了能原样解回来", back == payload, repr(sorted(back)))
    check("★ 密文里没有哨兵串（真的加密了）", SENTINEL not in json.dumps(blob),
          "明文漏进了 blob")


def test_wrong_password_is_refused():
    """口令错 -> `ValueError`（不是回一个空字典 —— 那会静默清空本机）。"""
    from core import settings_transfer as ST
    blob = ST.encrypt({"env": "X=1"}, "right")
    try:
        ST.decrypt(blob, "wrong")
        got = "没抛"
    except ValueError as exc:
        got = str(exc)
    check("★★ 口令错被拒", got != "没抛", got)
    check("★ 空口令也拒（不许默默给明文）", _raises(ST.decrypt, blob, ""), "")


def _raises(fn, *a) -> bool:
    try:
        fn(*a)
    except ValueError:
        return True
    except Exception:                                        # noqa: BLE001
        return False
    return False


def test_the_bundle_hides_secrets_by_default():
    from core import settings_transfer as ST
    _write(".env", "DEEPSEEK_API_KEY=%s\n" % SENTINEL)
    bundle = ST.build_bundle({"web_token": SENTINEL}, app_version="t")
    check("★ 默认包 `credentials` 是 false", bundle.get("credentials") is False)
    check("★★ 默认包里搜不到哨兵串", SENTINEL not in json.dumps(bundle),
          "没有任何勾选就把凭据带出去了")
    try:
        ST.build_bundle({}, include_credentials=True, password="")
        ok = False
    except ValueError:
        ok = True
    check("★★ 勾了凭据却不给口令 -> 拒", ok)

    enc = ST.build_bundle({"web_token": SENTINEL}, include_credentials=True,
                          password="pw")
    check("★ 勾了才有 enc 段", isinstance(enc.get("enc"), dict))
    names = enc.get("credential_names") or []
    check("★ 包里有一份名字清单（给用户看「带了什么」），且已排序",
          names == sorted(names) and "web_token" in names and "env" in names,
          repr(names))
    check("★★ 而它的密文之外仍搜不到哨兵串",
          SENTINEL not in json.dumps({k: v for k, v in enc.items()
                                      if k != "enc"}), "别的段漏了明文")


# 5. 形状 / 版本

def test_import_rejects_foreign_or_newer_bundles():
    from core import settings_transfer as ST
    p1 = ST.plan_import({"kind": "something-else", "schema": 1})
    check("★ 不是设置包 -> 拒", p1["ok"] is False and "错误包体" in p1["error"],
          repr(p1.get("error")))

    p2 = ST.plan_import(_minimal_bundle(schema=ST.SCHEMA + 1))
    check("★★ schema 比本程序新 -> 拒（不猜）",
          p2["ok"] is False and "优先更新程序" in p2["error"], repr(p2.get("error")))

    p3 = ST.plan_import(_minimal_bundle(schema=ST.SCHEMA))
    check("★ 同龄的包收下", p3["ok"] is True, repr(p3.get("error")))


# 6 / 7. 计划

def test_the_plan_lists_only_real_changes():
    from core import settings_transfer as ST
    bundle = _minimal_bundle(settings={"stream_output": True,
                                       "memory_write": True,
                                       "trace_full": False})
    cur = {"stream_output": False, "memory_write": True, "trace_full": False}
    plan = ST.plan_import(bundle, cur)
    check("★ 会变的进 changed（[现在, 包里]）",
          plan["changed"].get("stream_output") == [False, True],
          repr(plan["changed"].get("stream_output")))
    check("★ 已经一样的**不**进 changed",
          "memory_write" not in plan["changed"]
          and "trace_full" not in plan["changed"], repr(sorted(plan["changed"])))
    check("★ 数得清「没变」的格子", plan["unchanged"] == 2, repr(plan["unchanged"]))


def test_machine_keys_need_an_explicit_tick():
    """`boot_on_startup` 那句"要改注册表"的机械形态。

    默认一项都不应用 —— 那是这台机器的事，其中一格还会写注册表。
    """
    from core import settings_transfer as ST
    bundle = _minimal_bundle(settings={"boot_on_startup": True,
                                       "proxy_mode": "manual",
                                       "stream_output": True})
    cur = {"boot_on_startup": False, "proxy_mode": "system",
           "stream_output": False}
    plan = ST.plan_import(bundle, cur)
    check("★★ 机器项进了 `machine`（不混进 changed）",
          set(plan["machine"]) == {"boot_on_startup", "proxy_mode"},
          repr(sorted(plan["machine"])))
    check("★ 普通偏好照旧进 changed",
          "stream_output" in plan["changed"], repr(sorted(plan["changed"])))
    check("★ 没勾 -> 一项都不选", plan["machine_selected"] == [])

    out = ST.apply_import(bundle, plan, cur)
    check("★★ 没勾 -> 机器项一个都没落地",
          out["settings"].get("boot_on_startup") is False
          and out["settings"].get("proxy_mode") == "system",
          repr({k: out["settings"].get(k) for k in ("boot_on_startup", "proxy_mode")}))
    check("★ 而普通偏好落地了", out["settings"].get("stream_output") is True)

    plan2 = ST.plan_import(bundle, cur, selections=["boot_on_startup"])
    check("★ 勾了才选中它", plan2["machine_selected"] == ["boot_on_startup"],
          repr(plan2["machine_selected"]))
    out2 = ST.apply_import(bundle, plan2, cur, selections=["boot_on_startup"])
    check("★★ 勾了才真的写进去", out2["settings"].get("boot_on_startup") is True)
    check("★ 没勾的那一格仍然不动", out2["settings"].get("proxy_mode") == "system")


def test_a_foreign_bundle_cannot_write_local_only_keys():
    """包里硬塞 `web_token` / `workspace` 也不认（防御性，不是礼貌）。"""
    from core import settings_transfer as ST
    bundle = _minimal_bundle(settings={"web_token": "HACKED",
                                       "workspace": "C:/evil",
                                       "skill_configs": {"x": 1},
                                       "memory_write": False})
    cur = {"web_token": "MINE", "workspace": "D:/mine",
           "skill_configs": {"mail_parser": {}}, "memory_write": True}
    plan = ST.plan_import(bundle, cur)
    check("★ 计划里没有它们", "web_token" not in plan["changed"]
          and "workspace" not in plan["changed"], repr(sorted(plan["changed"])))
    check("★ 而且报成 skipped（不许静默忽略）",
          {"web_token", "workspace", "skill_configs"} <= set(plan["skipped"]),
          repr(plan["skipped"]))
    out = ST.apply_import(bundle, plan, cur)
    check("★★ 真写之后本机那三格原样",
          out["settings"]["web_token"] == "MINE"
          and out["settings"]["workspace"] == "D:/mine"
          and out["settings"]["skill_configs"] == {"mail_parser": {}},
          repr({k: out["settings"].get(k) for k in ("web_token", "workspace")}))
    check("★ 而普通偏好照常导入", out["settings"]["memory_write"] is False)


# 8. 真写

def test_apply_writes_allowlist_and_merges_the_ledger():
    from core import settings_transfer as ST
    from core import allowlist as AL
    from core import model_configs as MC

    _reset_model_configs()
    MC.create(card="deepseek", name="local-one", model="deepseek-chat")
    bundle = _minimal_bundle(
        allowlist={"egress": ["example.com"], "command": [], "email_to": [],
                   "computer_app": []},
        model_configs={"configs": {
            "imp00001": {"id": "imp00001", "card": "deepseek",
                         "name": "from-bundle", "model": "gpt-imported"},
        }, "default": ""},
    )
    plan = ST.plan_import(bundle, {})
    out = ST.apply_import(bundle, plan, {})
    check("★ 白名单落了", AL.list_for("egress") == ["example.com"],
          repr(AL.list_for("egress")))
    _reset_model_configs()
    ids = MC.all_entries()["configs"]
    check("★★ 包里的条目进来了", "imp00001" in ids, repr(sorted(ids)))
    check("★★ 本机原来那条**还在**（导入是并，不是替换）",
          any(e.get("name") == "local-one" for e in ids.values()),
          repr([e.get("name") for e in ids.values()]))
    check("★ 计数说得清", (out["applied"].get("model_configs") or {}).get("added") == 1,
          repr(out["applied"].get("model_configs")))


def test_apply_never_writes_the_settings_file():
    """`settings.json` 归端点写（它还要跑收尾）—— 这里不许偷偷写。"""
    from core import settings_transfer as ST
    target = Path("data/settings.json")
    before = target.read_text(encoding="utf-8") if target.exists() else None
    bundle = _minimal_bundle(settings={"stream_output": True})
    plan = ST.plan_import(bundle, {"stream_output": False})
    ST.apply_import(bundle, plan, {"stream_output": False})
    after = target.read_text(encoding="utf-8") if target.exists() else None
    check("★ 文件一个字节没动", before == after)


# 9. 端点

def _boot():
    import web.server as S
    tmp = Path(tempfile.mkdtemp(prefix="lv_xfer_")) / "settings.json"
    S._SETTINGS_PATH = tmp
    S.app.config["TESTING"] = True
    return S, S.app.test_client(), S._LAUNCH_TOKEN, tmp


def test_the_endpoints_need_a_token_and_default_to_dry_run():
    S, c, token, tmp = _boot()
    check("★ 导出没带 token -> 403",
          c.post("/api/settings/export", json={}).status_code == 403)
    check("★ 导入没带 token -> 403",
          c.post("/api/settings/import", json={}).status_code == 403)

    r = c.post("/api/settings/export?token=" + token, json={})
    d = r.get_json() or {}
    check("★ 导出 200 且有包", r.status_code == 200 and (d.get("bundle") or {}).get("kind")
          == "crannia-settings", str(r.status_code))
    bundle = d.get("bundle") or {}
    check("★ 导出的包不含凭据", bundle.get("credentials") is False)

    r = c.post("/api/settings/export?token=" + token,
               json={"include_credentials": True})
    check("★★ 勾了凭据不给口令 -> 400（不是默默不带）",
          r.status_code == 400, str(r.status_code))

    b = _minimal_bundle(settings={"stream_output": True},
                        model_configs={"configs": {}, "default": ""},
                        allowlist={"egress": [], "command": [], "email_to": [],
                                   "computer_app": []})
    _reset_model_configs()
    r = c.post("/api/settings/import?token=" + token, json={"bundle": b})
    d = r.get_json() or {}
    check("★★ 不传 dry_run -> **只出计划**（安全轴兜底）",
          r.status_code == 200 and d.get("applied") is False, str(r.status_code))
    check("★★ 而且真的一个字节都没写", not tmp.exists(), str(tmp))

    r = c.post("/api/settings/import?token=" + token,
               json={"bundle": b, "dry_run": False})
    d = r.get_json() or {}
    check("★ 确认之后落盘", d.get("applied") is True, repr(d.get("error")))
    now = json.loads(tmp.read_text(encoding="utf-8")) if tmp.exists() else {}
    check("★ 设置真的写进去了", now.get("stream_output") is True, repr(now))

    r = c.post("/api/settings/import?token=" + token,
               json={"bundle": {"kind": "nope"}, "dry_run": False})
    check("★ 不是设置包 -> 400", r.status_code == 400, str(r.status_code))


def test_the_endpoint_round_trips_an_export():
    """导出再导入 = 不变（包自己就是它的输入）。"""
    S, c, token, tmp = _boot()
    tmp.write_text(json.dumps({"stream_output": True, "trace_full": False}),
                   encoding="utf-8")
    _reset_model_configs()
    d = (c.post("/api/settings/export?token=" + token, json={}).get_json() or {})
    bundle = d.get("bundle") or {}
    r = c.post("/api/settings/import?token=" + token,
               json={"bundle": bundle, "dry_run": True})
    plan = r.get_json() or {}
    check("★★ 自己导出的包再导进来 -> 没有一格要变",
          plan.get("changed") == {}, repr(plan.get("changed")))
    check("★ 白名单也没有差异", plan.get("allowlist") == {},
          repr(plan.get("allowlist")))


def main() -> int:
    print("=" * 62)
    print("W92 batch 2: settings export / import")
    print("=" * 62)
    with sandbox():
        for fn in (test_the_bundle_never_carries_local_or_derived_keys,
                   test_the_bundle_fills_in_the_factory_baseline,
                   test_credentials_are_encrypted_and_round_trip,
                   test_wrong_password_is_refused,
                   test_the_bundle_hides_secrets_by_default,
                   test_import_rejects_foreign_or_newer_bundles,
                   test_the_plan_lists_only_real_changes,
                   test_machine_keys_need_an_explicit_tick,
                   test_a_foreign_bundle_cannot_write_local_only_keys,
                   test_apply_writes_allowlist_and_merges_the_ledger,
                   test_apply_never_writes_the_settings_file,
                   test_the_endpoints_need_a_token_and_default_to_dry_run,
                   test_the_endpoint_round_trips_an_export):
            print("")
            print("-- %s --" % fn.__name__)
            try:
                fn()
            except Exception as exc:                          # noqa: BLE001
                import traceback
                traceback.print_exc()
                check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                      False, str(exc))
    print("")
    if _FAILED:
        print("FAILED:")
        for x in _FAILED:
            print("  - %s" % x)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
