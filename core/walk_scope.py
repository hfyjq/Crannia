"""扫描范围：`glob` / `grep` 在走树时不进入哪些条目。

## 它解决什么

按模式扫描如果走进依赖目录与构建产物目录，结果会被第三方文件淹没，
而且大型目录树会让一次调用变慢。`core.workspace.skip_walk_entry` 只管
`.` / `$` / `_wip_` 三前缀（它同时服务于文件索引，改动会连带改变索引），
所以扫描另加两层判据，都只作用于扫描：

1. 固定的目录名（:data:`SCAN_SKIP_DIRS`）：依赖与构建产物。
2. `.gitignore`（:class:`ScanScope`）：从扫描根开始，逐目录累积规则。

## 边界

- 只影响扫描。`read_file` / `write_file` / `edit_file` 走的是显式路径，
  不受本模块管辖 —— 知道确切路径时永远读得到。
- 忽略不等于删除：被忽略的条目只是不进结果集，并且要报数
  （调用方把 `skipped_*` 写进回执），否则「没看到」与「不存在」会被读成同一件事。
- 可以退出去：`include_ignored=True` 时两层判据全部关闭，只保留
  `skip_walk_entry`。
- `.gitignore` 只从扫描根及其子孙读取。扫描根本身是仓库子目录时，
  仓库根那一份不生效（要它生效就把根指到仓库根）。

## `.gitignore` 支持的语法

`#` 注释 · 空行 · `!` 取反 · 尾 `/` 只匹配目录 · 首 `/` 或中段 `/` 锚定 ·
`*` / `?` / `` / `[...]`。取反后仍然不能把被排除目录内部**的东西找回来
（被排除的目录不会被进入，与 git 的行为一致）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

__all__ = [
    "SCAN_SKIP_DIRS", "gitignore_rules", "gitignore_for", "ignored",
    "skips_dir", "skips_file",
]

#: 扫描时不进入的目录名。判据是「生成物或第三方副本」——
#: 它们的内容不由本仓作者维护，搜索命中没有意义。
#: 故意不含 `build` / `out` / `bin`：这三个在非代码工作区里
#:    也常被当作普通目录名，而扫描结果里少掉一整棵子树是看不见的。
SCAN_SKIP_DIRS = frozenset({
    "node_modules", "bower_components", "jspm_packages",
    "__pycache__", "venv", "virtualenv", "site-packages",
    "dist", "target", "coverage", "htmlcov",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".gradle",
})


class _Rule:
    """一条 `.gitignore` 规则。`regex` 匹配相对于扫描根的 posix 路径。"""

    __slots__ = ("base", "regex", "negate", "dir_only")

    def __init__(self, base: str, regex: "re.Pattern[str]", negate: bool,
                 dir_only: bool):
        self.base = base            # 该 .gitignore 所在目录（相对扫描根，`""` 或 `a/b/`）
        self.regex = regex
        self.negate = negate
        self.dir_only = dir_only

    def hits(self, rel_path: str, is_dir: bool) -> bool:
        if self.base and not rel_path.startswith(self.base):
            return False
        direct = self.regex.search(rel_path) is not None
        if direct:
            # 只匹配目录的规则命中一个文件时不算 —— 那是同名的文件，不是那个目录。
            return (not self.dir_only) or is_dir
        if not self.dir_only:
            return False
        # 只匹配目录的规则：祖先目录命中 = 这个条目命中。
        # 走树时命中的目录会被整棵跳过，所以这条在文件上通常不会走到；
        # 留着是因为单独调用判定函数时，两种问法必须给出同一个答案。
        parts = rel_path.split("/")
        for i in range(1, len(parts)):
            if self.regex.search("/".join(parts[:i])) is not None:
                return True
        return False


def _translate(pat: str) -> str:
    """把一段 glob 翻成正则片段。只处理 gitignore 用得上的那些元字符。"""
    out: list[str] = []
    i, n = 0, len(pat)
    while i < n:
        ch = pat[i]
        if ch == "*":
            if pat.startswith("**", i):
                j = i + 2
                if j < n and pat[j] == "/":
                    out.append("(?:.*/)?")     # `**/` 可匹配零层
                    i = j + 1
                    continue
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
            i += 1
            continue
        if ch == "?":
            out.append("[^/]")
            i += 1
            continue
        if ch == "[":
            close = pat.find("]", i + 1)
            if close == -1:
                out.append(r"\[")
                i += 1
                continue
            body = pat[i + 1:close]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body + "]")
            i = close + 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(re.escape(pat[i + 1]))
            i += 2
            continue
        out.append(re.escape(ch))
        i += 1
    return "".join(out)


def _compile(pat: str, base: str) -> Optional["re.Pattern[str]"]:
    """一条规则 → 正则。锚定与否按 gitignore 的判据（首 `/` 或中段 `/`）。"""
    anchored = pat.startswith("/")
    if anchored:
        pat = pat[1:]
    dir_only = pat.endswith("/")
    if dir_only:
        pat = pat[:-1]
    if not pat:
        return None
    body = _translate(pat)
    lead = re.escape(base)
    if "/" in pat or anchored:
        expr = "^" + lead + body
    else:
        expr = "^" + lead + r"(?:.*/)?" + body
    if dir_only:
        expr += r"/?$"
    else:
        expr += r"(?:/.*)?$"     # 目录命中时整棵子树都算命中
    try:
        return re.compile(expr)
    except re.error:
        return None


def gitignore_rules(text: str, base: str) -> list:
    """解析一份 `.gitignore` 文本，产出规则列表。`base` 是该文件所在目录。

    末条规则优先（与 git 一致）：调用方倒着扫，第一条命中即结论。
    """
    rules: list = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        negate = line.startswith("!")
        if negate:
            line = line[1:]
        line = line.strip()
        if not line:
            continue
        rx = _compile(line, base)
        if rx is not None:
            rules.append(_Rule(base, rx, negate, line.endswith("/")))
    return rules


def gitignore_for(dir_path: Path, rel_base: str) -> tuple:
    """读一个目录的 `.gitignore`，返回可追加到规则链尾部的元组。

    读不到（没有文件 / 权限 / 坏编码），空元组，不抛：忽略规则读不到时
    的合理行为是「这一层没有额外忽略」，而不是让整次扫描失败。
    """
    try:
        gi = Path(dir_path) / ".gitignore"
        if not gi.is_file():
            return ()
        text = gi.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(gitignore_rules(text, rel_base))


def ignored(rules: Iterable, rel_path: str, is_dir: bool) -> bool:
    """按规则链判断一个相对路径是否被忽略。末条优先。"""
    for rule in reversed(tuple(rules)):
        if rule.hits(rel_path, is_dir):
            return not rule.negate
    return False


def skips_dir(rules: Iterable, name: str, rel_path: str) -> bool:
    """扫描时要不要整棵跳过这个目录。"""
    return name in SCAN_SKIP_DIRS or ignored(rules, rel_path, True)


def skips_file(rules: Iterable, rel_path: str) -> bool:
    return ignored(rules, rel_path, False)
