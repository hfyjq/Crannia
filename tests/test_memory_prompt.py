"""
主 agent 的 system prompt 里必须带着记忆纪律。

为什么需要它
============
记忆有两条写路：主 agent 自己调 `memory_write`，以及后台 fork 抽取。
如果主 agent 手里没有"什么时候该记"的准则，它会自己发明一套
（例如"等用户确认再补记"），而后台抽取在用户确认之前就已经写了 ——
两条写路对用户说的不是同一句话。所以纪律必须写在主 agent 的 system prompt 里，
而不是只放在 fork 的 directive 里。

三处刻意不做的承诺（提示词里不许出现）：
① 索引是派生的，不许让模型「往 `MEMORY.md` 里加一行指针」；
   写成那样就是让模型去手工维护一个生成物。
② 工具层没有删除（`EXTRACT_ALLOWLIST` / 三件工具都没有 delete），
   只能写"去设置页删"，不能写"找到并删掉"。
③ 计划模式的声明不进 system prompt —— 实测把模式声明每请求写进前缀
   会让模型瘫痪（12 轮里 5 轮以零工具调用结束），不许出现
   `exit_plan_mode` / `enter_plan_mode`。

这个文件里最承重的一条是 `test_named_tools_actually_exist`：
提示词里点名的工具必须真的存在 —— 别承诺兑现不了的。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_memory_prompt.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


#: 四类不是工具，是记忆的 type 值 —— 反引号里出现它们是对的。
#: 这张表必须是显式白名单：否则读者会为了绕开误报而放宽正则，
#: 而那等于把这条断言废掉（没有读者的清单会烂）。
_TYPE_WORDS = frozenset({"user", "feedback", "project", "reference"})

_HDR = "## LONG-TERM MEMORY"

#: 反引号里点名工具的两种写法：`name` 与 `name(args)`。
_TOOL_RE = re.compile(r"`([a-z][a-z0-9_]*)(?:\([^`]*\))?`")


#: `<git_status>` 那一块不是指令，是环境事实 ——
#: 它的内容是 `git log --oneline -5`，也就是最近五个提交的标题，
#: 会原样出现在 `assemble_system_prompt("")` 的结果里。
#:于是"某次提交的标题里写了 `exit_plan_mode`"会让下面那条
#: 「提示词里不许出现计划模式」误报。
#: 断言的对象是静态指令；把 git 快照一起扫进来，等于让仓库的提交历史
#: 成为这条护栏的输入 —— 那不是它的判据。
#: （现象是真红，但根因在探针，不在被测物。）
_GIT_BLOCK_RE = re.compile(r"<git_status>.*?</git_status>", re.S)


def _system_prompt() -> str:
    from core.prompt_assembler import assemble_system_prompt
    return _GIT_BLOCK_RE.sub("", assemble_system_prompt(""))


def _named_tools(text: str):
    """反引号里那些看起来是工具的词（去掉四类名）。"""
    toks = {m.group(1) for m in _TOOL_RE.finditer(text)}
    return sorted(toks - _TYPE_WORDS)


def _registered_tool_names():
    """已注册的工具名。走生产同一条装载路径，不抄第二份。"""
    from measure_request import boot_registry
    boot_registry()
    from bus.registry import get_tool_schemas
    return {td["function"]["name"] for td in get_tool_schemas()}


# ----------------------------------------------------------------------

def test_block_is_present():
    assert _HDR in _system_prompt(), (
        "主 agent 的 system prompt 里没有记忆纪律段 —— W40 被摘掉了")


def test_named_tools_actually_exist():
    """承重的那条：提示词里点名的工具必须真的存在。

    前提断言是防空跑：注册表没装全时这条会"绿"，而它绿得毫无意义。
    """
    text = _system_prompt()
    named = _named_tools(text)
    assert len(named) >= 4, (
        "只从提示词里认出 %d 个工具名 —— 提取器坏了，这条断言会空跑" % len(named))

    # 不能包在 `sandbox()` 里 —— 装载要读真的 `skills/`，
    #    而 `sandbox()` 把 cwd 挪进临时目录，`FileNotFoundError: 'skills'`。
    registered = _registered_tool_names()
    assert len(registered) >= 20, (
        "只装载到 %d 个工具 —— 注册表没装全，这条断言会空跑" % len(registered))

    missing = [n for n in named if n not in registered]
    assert not missing, (
        "提示词点名了不存在的工具 %r —— 那是**空头支票**（D7 族）" % missing)


def test_negative_control_fake_tool_is_caught():
    """自造靶子：给提取器喂一个编造的工具名，它必须挑得出来。

    没有这条，`test_named_tools_actually_exist` 的判别力寄存于未来
    （今天一个假名字都没有，它今天恒真）。
    """
    registered = {"memory_read", "memory_write"}
    fake_text = "use `definitely_not_a_tool` and `memory_read` for that"
    named = _named_tools(fake_text)
    missing = [n for n in named if n not in registered]
    assert missing == ["definitely_not_a_tool"], (
        "负对照没打中：提取器要么漏了假名字，要么把真名字也判成假的 -> %r" % (named,))


def test_does_not_promise_unbuilt_capabilities():
    """提示词里不许提计划模式。

    理由：计划模式的声明绝不能进 system prompt。实测把「命令在只读
    文件沙箱下运行」这句话每请求都写进前缀，模型瘫痪
    （首次人工会话 12 轮里 5 轮以零工具调用结束）。
模式走另外两条路：拒绝那一刻的话术 + 工具回执，都不走这里。
    另两条独立理由（缓存前缀 / 每轮重发的纯税）见 `core/plan.py` 的 docstring。
    """
    text = _system_prompt()
    for bad in ("exit_plan_mode", "enter_plan_mode", "Plan mode", "plan mode"):
        assert bad not in text, (
            "提示词里出现了计划模式（%s）—— 模式声明走「拒绝那一刻」与工具回执，"
            "**绝不进 system prompt**（实测：放前缀里会让模型瘫痪）" % bad)


def test_index_is_derived_not_hand_maintained():
    """我们的索引是派生的。

    如果索引是真文件，模型就得手工往 `MEMORY.md` 里加一行指针；
    我们的不是（`build_memory_index()` 每轮现算）。
    照抄那一句，模型会去维护一个它碰不到、也不需要维护的东西。

    「别手工维护索引」这句话住在 `memory_write` 的工具描述里
    （判据：它就该在读的人正要动手的那一刻出现），所以下面两处都要查。
    """
    # ① prompt 里不许出现 `MEMORY.md`（照抄手工索引那条路）
    text = _system_prompt()
    assert "MEMORY.md" not in text, (
        "我们的索引是派生的，模型不该去维护它")
    # ② 那句「别手工维护索引」仍然要给模型看，只是换了地方
    from core.memory_store import MEMORY_WRITE_SCHEMA
    desc = MEMORY_WRITE_SCHEMA["description"]
    assert "别手工维护" in desc, (
        "「别手工维护索引」这句没有落到工具描述里 —— 缺了它，模型可能反着理解")


def test_delete_tool_is_promised_and_backed():
    """删除工具有了，提示词要说它的名字，而且那个名字真的注册过。

    反方向也要钉：不许承诺一个调不动的工具 —— 光在提示词里写
    `memory_delete` 而注册表里没有，是把"它有这个能力"变成一句空话。
    """
    text = _system_prompt()
    assert "memory_delete" in text, "提示词没给模型一条真的删除路线"
    assert "memory_search" in text, "没交代「不确定先搜」"
    from core.memory_store import MEMORY_DELETE_SCHEMA
    assert MEMORY_DELETE_SCHEMA["name"] == "memory_delete"
    from pathlib import Path
    reg = (Path(__file__).resolve().parent.parent / "bus" / "registry.py").read_text(
        encoding="utf-8")
    assert 'register_tool("memory_delete"' in reg, "名字没被注册 —— 那是空头支票"


def test_credentials_are_excluded():
    """凭证永远不许进记忆库。"""
    text = _system_prompt()
    assert "Never save credentials" in text and "API keys" in text


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    ok = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            print("FAIL  %s: %s" % (name, exc))
        except Exception as exc:  # noqa: BLE001
            print("ERROR %s: %s: %s" % (name, type(exc).__name__, exc))
        else:
            print("PASS  %s" % name)
            ok += 1
    print()
    print("%d/%d passed" % (ok, len(tests)))
    return 0 if ok == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_main())
