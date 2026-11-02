"""桌面端导航的源级读者。

## 它为什么存在

失败形状：**无论定时任务还是 skill 或者端配置页，只要不在新建对话页，
点击旧对话就没反应**。根因是一条漏写：

    `openThread()` 把消息、`viewing` 都设好了，却没有 `setPage("chat")`
人还站在原来那一屏上，数据加载了，界面什么都没发生。

React 层没有行为读者，这类"少一行"的错只能靠实际走查。
本文件不是要把那个缺口补上（源级检查替代不了走查），它盯的是同一个形状：
"点下去该有去处"这件事，没有任何东西在管。

## 两条判据（都往两边查）

  ① `openThread` 里必须有 `setPage("chat")`。
  ② NAV 的每一项都要有落点，且落点要与"未实现"角标一致：
     · 挂着「未实现」的，不许有真的渲染分支（那会让一屏能用的东西自称不能用，
       「标签在说谎」同族）；
     · 没挂的，必须有渲染分支或一个明确去处（`endpoint` 开系统浏览器），
       否则点进去是一片 `Stub`，而页签上什么都没说。
     这一条是两向的：只做一向，另一个方向会悄悄烂。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_desktop_nav.py

[!] 输出一律 ASCII 安全字符（Windows 控制台默认 GBK）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APP = _ROOT / "packages" / "ui" / "src" / "App.jsx"
_NAV = _ROOT / "packages" / "ui" / "src" / "SchedulesPanel.jsx"

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
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


def _src() -> str:
    return _APP.read_text(encoding="utf-8", errors="replace")


def _strip_comments(src: str) -> str:
    """先剥注释 —— 描述旧形状的注释会让检查器自己打自己。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    return src


def _nav_keys(src: str) -> list:
    """`[(key, has_badge), …]` —— 从 `const NAV = [...]` 那个块里抠。

    一行可能有不止一项（`["chat", …], ["skills", …],`）—— 按行
    取"行首那个 `[`"会漏掉 skills，还会把 `schedules` 判成挂着角标
    （那一行上真正挂角标的是它后面那个 `library`）。必须逐个 `[…]` 匹配。
    """
    m = re.search(r"const NAV = \[(.*?)\n\];", src, flags=re.S)
    if not m:
        return []
    out = []
    for entry in re.finditer(r'\["([a-z_]+)",[^\]]*\]', m.group(1)):
        out.append((entry.group(1), "未实现" in entry.group(0)))
    return out


def _render_keys(src: str) -> set:
    """出现在渲染分支里的 key：`page === "x"`。"""
    return set(re.findall(r'page === "([a-z_]+)"', src))


# ------------------------------------------------------------------

def test_open_thread_switches_to_the_chat_page():
    """`openThread` 必须切屏。

    没有它，"人站在别的页签时点侧栏旧对话" = 数据加载了、界面什么都没发生。
    """
    body = _strip_comments(_src())
 # 调整：形参改名，避免遮蔽 i18n 的 `t`；函数体里
    #:   有 `t("（这条会话没有用户消息）")` —— 没标题的会话点进去就 TypeError。
    m = re.search(r"async function openThread\((?:t|th)\)\s*\{(.*?)\n  \}", body, flags=re.S)
    check("找得到 `openThread` 的定义（找不到 = 它改名了，这条会空转）",
          m is not None, "")
    if not m:
        return
    fn = m.group(1)
    check("★★ `openThread` 里**必须**有 `setPage(\"chat\")`（点了要有反应）",
          'setPage("chat")' in fn, fn[:200])
    # 位置：要在取数之后（`viewLoading` 只禁用按钮、没有 loading 提示，
    # 提前切屏会先闪一眼上一条会话）
    i_call = fn.find("/api/messages")
    i_page = fn.find('setPage("chat")')
    check("★ 切屏排在**取数之后**（否则先闪一眼上一条会话，比'没反应'更像 bug）",
          i_call != -1 and i_page != -1 and i_page > i_call,
          "api=%d page=%d" % (i_call, i_page))
    check("★ 两者在**同一个 try 的同一批**里（`setMessages` 也在那儿）",
          'setPage("chat");' in fn and "setMessages(" in fn, "")


def test_every_nav_entry_has_a_destination_consistent_with_its_badge():
    """两向：挂着「未实现」的别有真页面 · 没挂的必须有去处。"""
    src = _strip_comments(_src())
    entries = _nav_keys(src)
    rendered = _render_keys(src)
    check("解析到 NAV 的项（否则下面两条会空转）", len(entries) >= 4, repr(entries))
    if not entries:
        return

    #: 不走 `setPage` 的那一项（它不是这个 SPA 里的一屏，
    #:   而是开系统浏览器去 WebUI 那一页）。
    SPECIAL = {"endpoint"}

    for key, has_badge in entries:
        if has_badge:
            check("NAV「%s」挂着「未实现」-- **不许**有真渲染分支" % key,
                  key not in rendered,
 "它有分支，却还自称未实现 —— 那一屏能用的东西在自称不能用")
        else:
            check("NAV「%s」没挂角标 -- 必须有去处（渲染分支或特例）" % key,
                  key in rendered or key in SPECIAL,
                  "点它进一片 Stub，而页签上什么都没说 —— 那就是'点了没反应'的另一种写法")

    check("反向：渲染分支里的 key 都是 NAV 里有的（别留孤儿分支）",
          rendered <= {k for k, _ in entries},
          repr(rendered - {k for k, _ in entries}))


def test_the_schedules_entry_is_now_a_real_page():
    """定时任务摘掉角标 + 真有面板 —— 两条要一起成立。"""
    src = _strip_comments(_src())
    entries = dict(_nav_keys(src))
    check("NAV 里「schedules」不再挂「未实现」", entries.get("schedules") is False,
          repr(entries.get("schedules")))
    check("★ 而且真有渲染分支（不是删了角标却还是一片 Stub）",
          "schedules" in _render_keys(src), "")
    check("★ 那个分支渲染的是新面板",
          re.search(r'page === "schedules"\s*\?\s*<Schedules', src) is not None, "")
    check("新面板文件在", _NAV.exists(), str(_NAV))
    panel = _NAV.read_text(encoding="utf-8", errors="replace") if _NAV.exists() else ""
    check("★ 面板的规则校验/预览走**后端**（前端不解释规则）",
          "api/cron/preview" in panel, "")


def main() -> int:
    for fn in (test_open_thread_switches_to_the_chat_page,
               test_every_nav_entry_has_a_destination_consistent_with_its_badge,
               test_the_schedules_entry_is_now_a_real_page):
        try:
            fn()
        except Exception as exc:                      # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__), False, str(exc))

    print("\n" + "=" * 62)
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
