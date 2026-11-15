"""编程纪律：改完要验。

两件事，共用同一份回合内状态（`ProgramTurn`）：

1. 改完给路线：本回合第一次写代码文件成功时，回执末尾附一条该项目的
   验证命令；
2. 交卷前闸：本回合写过代码、却一条命令都没跑过，在模型决定收工
   那一刻提醒一次。

## 判据

- 只认代码类扩展名（`CODE_EXTS` 白名单）。改文档、配置、数据不该被
  催着跑测试 —— 那会把提醒变成噪声，而天天出现的话就没有人再读它。
- 探测不到验证命令就一个字都不说（不给"记得验证一下"这种空话）。
- 每个回合最多提醒一次：模型回一句"这次是纯文档改动"就放行，不追第二遍。

## 为什么"跑过命令"不区分命令种类

区分需要解析命令，而本仓库不写命令解析器（`core/command_policy.py` 的判据：
解析必然漏判）。所以这里的判据刻意停在"一条命令都没跑"这一档 ——
只抓最明显的那个洞：改完一堆文件、然后没有任何执行记录就交付。
误差方向是"少提醒"，不是"乱提醒"。

## 成本

- 不占系统提示、不进工具 schema：每条请求零常驻 token。
- 只在"写过代码文件"时才做一次目录探测（几个 `stat`）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

__all__ = [
    "CODE_EXTS", "NUDGE_TEXT", "is_code_path", "verification_command",
    "ProgramTurn",
]

#: 代码类扩展名 —— 有编译或运行行为、".".join 之后能被验证的那些。
#: 刻意不含 `.md` / `.txt` / `.json` / `.yaml` / `.toml` / `.css` /
#: `.html`：改这些不该触发"去跑测试"。
CODE_EXTS = frozenset({
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs",
    ".java", ".kt", ".kts", ".scala", ".swift", ".m", ".mm",
    ".rb", ".php", ".lua", ".gd", ".sh", ".bash", ".ps1", ".sql",
})

#: 交卷前那一句。只在该触发时才进历史，所以可以写清楚；但仍然短。
NUDGE_TEXT = (
    "（系统提示：本回合改过代码文件，但**一条命令都没有运行过**。"
    "如果这次是纯文档/配置改动、不需要执行，就直接说明；"
    "否则先跑一次验证（测试、构建或运行），再交付结论。）"
)

#: 探测到的项目类型 -> 验证命令。顺序即优先级。
_SIMPLE_MARKERS = (
    ("project.godot", "godot --headless --path . --quit"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
)

#: 向上找项目根的上限层数。超过它就不猜（别一路摸到盘根）。
_MAX_UP = 5


def is_code_path(path) -> bool:
    """这个路径是不是"改了就该跑一次"的代码文件。"""
    if not isinstance(path, (str, Path)) or not path:
        return False
    try:
        return Path(str(path)).suffix.lower() in CODE_EXTS
    except (ValueError, OSError):
        return False


def _npm_test_command(root: Path) -> str:
    """`package.json` 里真的声明了 test 脚本才给 —— 否则 `npm test` 是空跑。"""
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    scripts = data.get("scripts")
    if isinstance(scripts, dict) and str(scripts.get("test") or "").strip():
        return "npm test"
    return ""


def _has_pytest_marker(root: Path) -> bool:
    """有 pytest 的常见痕迹之一。

    不扫"任意 `test_*.py`" —— 那在每个目录上都要列一次目录项，
    而这里只值得花几个 `stat`。
    """
    return ((root / "tests").is_dir()
            or (root / "pytest.ini").is_file()
            or (root / "conftest.py").is_file()
            or (root / "tox.ini").is_file())


def verification_command(path) -> str:
    """从 *path* 往上找项目根，给一条具体的验证命令。

    找不到，返回空串（调用方据此什么都不说）。
    """
    if not isinstance(path, (str, Path)) or not path:
        return ""
    try:
        cur = Path(str(path)).parent
    except (TypeError, ValueError, OSError):
        return ""
    for _ in range(_MAX_UP):
        for marker, cmd in _SIMPLE_MARKERS:
            if (cur / marker).is_file():
                return cmd
        if (cur / "package.json").is_file():
            cmd = _npm_test_command(cur)
            if cmd:
                return cmd
        if ((cur / "pyproject.toml").is_file()
                or (cur / "setup.py").is_file()):
            if _has_pytest_marker(cur):
                return "python -m pytest -q"
        # 到仓库根就停：`.git` 在的项目根之外不该继续摸。
        if (cur / ".git").exists():
            break
        nxt = cur.parent
        if nxt == cur:
            break
        cur = nxt
    return ""


class ProgramTurn:
    """一个用户回合内的编程纪律状态（sync / stream 两条路各持一份）。"""

    def __init__(self) -> None:
        #: 本回合写过代码文件（文档/配置不算）。
        self.wrote_code = False
        #: 本回合真正执行过命令。
        self.ran_command = False
        #: 层 2 的那条路线已经给过了 —— 本回合只说一次。
        self._hint_given = False
        #: 层 1 的提醒已经给过了 —— 不追第二遍。
        self.nudged = False

    # -- 记录 ---------------------------------------------------------
    def observe(self, tool_name: str, result) -> str:
        """记一笔这次工具结果，返回要附到回执末尾的一句话（通常是空串）。

        只认真的执行成功的结果：`result` 是 dict、`success` 为真。
        被拒 / 挂起 / 异常那些分支拿到的不是这个形状。
        """
        if not isinstance(result, dict):
            return ""
        if tool_name == "run_command":
            # 挂起等审批那条没执行（模型拿到的是一张待审批载荷）。
            if result.get("status") == "pending_approval":
                return ""
            self.ran_command = True
            return ""
        if tool_name not in ("write_file", "edit_file"):
            return ""
        if not result.get("success"):
            return ""
        path = result.get("path")
        if not is_code_path(path):
            return ""
        self.wrote_code = True
        if self._hint_given:
            return ""
        self._hint_given = True
        cmd = verification_command(path)
        if not cmd:
            return ""
        return "本项目的验证命令：`%s`" % cmd

    # -- 判定 ---------------------------------------------------------
    def needs_verify(self) -> bool:
        """改过代码、却一条命令都没跑过。"""
        return bool(self.wrote_code and not self.ran_command)

    def take_verify_nudge(self) -> Optional[str]:
        """要提醒就返回那句提醒（并且只返回一次）；否则 `None`。"""
        if self.nudged or not self.needs_verify():
            return None
        self.nudged = True
        return NUDGE_TEXT
