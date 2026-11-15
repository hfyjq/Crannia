"""规避目录那张表的读写 —— 经 HTTP 层。

## 为什么要有它

用户报的现场：「规避目录的删除没效果」。根因在 GET 那一侧：

    GET 每次都把默认那一批并回用户表 -> 界面上删掉任何一条与默认重合的目录，
    下一次拉取它又回来了；而且每存一次，默认就被写进 `settings.json`，
    那一份从此不再是"用户的表"。

三条不变量，正反两向都钉：

1. 没配过才给默认那一批；而且不落盘（文件里不该凭空多出这个键）。
2. 配过（包括空表）就原样交出来 —— `GET 返回的表 == 文件里存的那张表`。
3. `null` = 恢复默认：键被移除，下一次 GET 又回到默认那一批。

第 2 条是这次修的正是它：旧的 merge 让这条恒假，而界面上只表现为"删了又回来"。

`_SETTINGS_PATH` 是项目根的绝对路径，sandbox 的 chdir 管不到它，
必须像 `test_ice_ui` 那样显式改掉，否则这个测试会改生产设置。
输出一律 ASCII（Windows 控制台 GBK）。
"""

#@exclusive: prod-fingerprint
# ↑ 它在读生产目录 —— 并行时别人的中间态会被拍进指纹，假红。

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from _sandbox import sandbox                                            # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + str(detail)) if detail else ""))


def _boot():
    """起一个真的 Flask test client，把落盘路径钉进临时目录。"""
    import web.server as S
    S._SETTINGS_PATH = Path.cwd() / "data" / "settings.json"
    if S._SETTINGS_PATH.exists():
        S._SETTINGS_PATH.unlink()
    S.app.config["TESTING"] = True
    return S, S.app.test_client(), S._LAUNCH_TOKEN


def _get(c, token):
    return c.get("/api/settings?token=" + token).get_json() or {}


def _post(c, token, payload):
    return c.post("/api/settings?token=" + token, json=payload)


def _stored(tmp_root: Path) -> dict:
    import json
    p = tmp_root / "data" / "settings.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# 一、没配过 -> 默认；而且不落盘

def test_fresh_install_gets_defaults_without_persisting(tmp_root):
    S, c, token = _boot()
    body = _get(c, token)
    got = body.get("forbidden_directories")
    from core.workspace import resolve_default_excludes
    check("GET 给出的是默认那一批", got == resolve_default_excludes(), repr(got)[:200])
    check("★ 而且**没有**被写进文件（读不到键 = 从没配过）",
          "forbidden_directories" not in _stored(tmp_root),
          repr(sorted(_stored(tmp_root).keys()))[:200])


# 二、删掉一条默认 -> 它真的没了（这就是用户报的那条）

def test_deleting_a_default_entry_sticks(tmp_root):
    S, c, token = _boot()
    from core.workspace import resolve_default_excludes
    defaults = resolve_default_excludes()
    check("先自证：默认那一批至少有两条（不然下面删的都是空的）",
          len(defaults) >= 2, repr(defaults)[:200])
    kept = defaults[1:]
    r = _post(c, token, {"forbidden_directories": kept})
    check("写进去 -> 200", r.status_code == 200, str(r.status_code))
    got = _get(c, token).get("forbidden_directories")
    check("★★ 删掉的那条**没有回来**", got == kept, repr(got)[:200])
    check("★★ 文件里存的就是用户那张表（默认没被并进去）",
          _stored(tmp_root).get("forbidden_directories") == kept,
          repr(_stored(tmp_root).get("forbidden_directories"))[:200])


def test_an_explicit_empty_list_is_empty(tmp_root):
    S, c, token = _boot()
    _post(c, token, {"forbidden_directories": []})
    got = _get(c, token).get("forbidden_directories")
    check("★ 空表就是空表（不许拿默认顶上）", got == [], repr(got)[:200])
    check("文件里存的也是空表", _stored(tmp_root).get("forbidden_directories") == [],
          repr(_stored(tmp_root).get("forbidden_directories")))


def test_null_restores_the_defaults(tmp_root):
    S, c, token = _boot()
    _post(c, token, {"forbidden_directories": []})
    r = _post(c, token, {"forbidden_directories": None})
    check("null -> 200", r.status_code == 200, str(r.status_code))
    check("★ 键被移除（不是存了一个 null）",
          "forbidden_directories" not in _stored(tmp_root),
          repr(_stored(tmp_root).get("forbidden_directories", "<absent>")))
    from core.workspace import resolve_default_excludes
    check("再 GET 回到默认那一批",
          _get(c, token).get("forbidden_directories") == resolve_default_excludes(),
          repr(_get(c, token).get("forbidden_directories"))[:200])


# 三、内存里那份也得跟着走（界面说删了，实际还在 = 标签在说谎）

def test_the_live_guard_follows_the_saved_list(tmp_root):
    from core.security import SecurityGuard
    from core.workspace import WorkspaceManager, resolve_default_excludes

    def _wm(settings):
        guard = SecurityGuard(allowed_dirs=["./workspace"], command_blacklist=[],
                              audit_log_path="./logs/audit.log")
        return WorkspaceManager(guard, settings or None)

    wm = _wm({"forbidden_directories": []})
    check("★★ 空表要**清空**内存里那份规避清单",
          wm._security.forbidden_dirs == [], repr(wm._security.forbidden_dirs)[:200])
    wm2 = _wm({"stream_output": True})
    names = {Path(x).name for x in wm2._security.forbidden_dirs}
    want = {Path(x).name for x in resolve_default_excludes()}
    check("★ 没有这个键时用的是默认那一批", want <= names,
          "缺：%r" % (sorted(want - names),))


# ----------------------------------------------------------------------

def main() -> int:
    from _sandbox import prod_snapshot, prod_diff
    before = prod_snapshot()
    with sandbox() as tmp:
        tests = [
            test_fresh_install_gets_defaults_without_persisting,
            test_deleting_a_default_entry_sticks,
            test_an_explicit_empty_list_is_empty,
            test_null_restores_the_defaults,
            test_the_live_guard_follows_the_saved_list,
        ]
        for fn in tests:
            try:
                fn(tmp)
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                      False, str(exc))
    leaked = prod_diff(before, prod_snapshot())
    check("没有污染生产目录", not leaked, str(leaked))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
