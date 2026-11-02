"""只读生成器 —— 提议每个工具的 `destructive:`。

与 `scripts/propose_permissions.py` 是同一台机器的第二个刻度盘
（那个问"改不改东西"，这个问"改得回来吗"）。扫描器是同一个，
只是换了动词表（`_write_marks(..., attrs=IRREVERSIBLE_ATTRS, extra_rules=False)`）。

## 它按什么提议，以及为什么这不是"把猜测抄一遍"

| 级 | 判据 | 提议 | 为什么这不是"搬家" |
|---|---|---|---|
| A | AST 命中不可逆原语 | 留空 | 真正的交付物 —— 7 个，逐条人要拿主意 |
| B | AST 干净 | `false` | 证据来自 `IRREVERSIBLE_ATTRS` 那条扫描，与"今天是什么"零关系 |

A 级留空是刻意的，理由同 `propose_permissions.py`：
让生成器把"现值"写进文件 = 抄一遍不算做过决定。

兜底方向决定了"留空"的代价比 `permission` 那边大：
`permission` 的兜底是 `admin`（fail-closed），`destructive` 的兜底是 `false`（fail-open）
一个没人看过的工具会静默地不算不可逆。这也是为什么第 5 步那把尺子非有不可
（`tests/test_destructive_declared.py`：AST 命中却没声明 `true`、又不在账本里，红）。

## 用法

```bash
.venv/Scripts/python.exe scripts/propose_destructive.py          # 只报，一个字都不写
.venv/Scripts/python.exe scripts/propose_destructive.py --apply  # A 级全部有答案之后才允许
```

`--apply` 是行级插入（插在那一行的 `permission:` 之后），不是 `yaml.safe_dump` 回写 ——
后者会把手写的注释、`>` 折行块、字段顺序全部推平。
manifest 是 CRLF（本仓混排，`core/file_tools.py` 那条纪律记着），插入行也必须 `\r\n`。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 复用那条现成的扫描器与两张动词表 —— 不抄第二份。
from tests.test_permission_tiers import (  # noqa: E402
    IRREVERSIBLE_ATTRS, _collect_defs, _write_marks,
)

#: A 级的答案 + 账本 —— 空着就说明还没决定；填了必须带理由。
#:
#: 格式：`工具名: (destructive, "为什么")`。
#: `False` 的那几条就是"账本"：扫描器说它命中不可逆原语，
#: 而人被它判成可逆，理由是必填的 —— 第 5 步那把尺子读的就是这张表
#: （`tests/test_destructive_declared.py`：命中了却没声明 `true`、又不在账本里，红）。
#: ## 判定规则（四个裁决都从它推出来 —— 要反驳就反驳这一条）
#:
#: > 不可逆 = 结果里有一部分是「调用者没要求的」损失。
#:
#: 这条规则决定了 `write_file` / `edit_file` 为什么是 `false`（覆盖**调用者点名要写的
#: 那个路径**是本意，不是附带损失），也决定了下面这三个"账本"条目。
#: 没有这条规则，闸（第 6 步）会为每一次写文件弹窗 —— 而那等于把闸关掉。
#:
#: 反过来说，`delete_files` 是 `true`（删掉就没有了，且没有东西替代它）、
#: `send_email` 是 `true`（发出去收不回来）、`run_command` 是 `true`（能做没人要求过的事）。
LEDGER: dict[str, tuple[bool, str]] = {
    # file_parser
    "delete_files": (True, "`p.unlink()` 删的是**用户的文件** —— 不可逆，且没有东西替代它。"
                           "★ 它已经在 `_APPROVAL_CAPABLE_TOOLS` 里（自己的发射点，"
                           "`skills/file_parser/handler.py:897`），所以兜底闸不会问它第二次。"),
    # mail_parser
    "send_email": (True, "`server.sendmail(...)` —— **发出去就收不回来**。"
                         "★★ 它是兜底闸今天**唯一**会对它生效的工具（爆炸半径 = 1）。"),
    # office_parser
    "create_document": (False, "**账本**：它自己**不**删东西（`target.unlink()` 那一跳来自 "
                               "`export_document`，它内部就调那个）。按判定规则：覆盖的是"
                               "**调用者点名要写的产出路径** -> 损失是本意，不是附带。"),
    "export_document": (False, "**账本**：`target.unlink()` + `candidate.rename(target)` —— "
                               "删的是**同名的上一版产物**（agent 自己刚建的工作文件正被改名成"
                               "成品名），两个端点都过 `validate_path(..., \"write\")`。"
                               "按判定规则：目标路径是**调用者点名的** -> 损失是本意。"),
    "rename_worksheet": (False, "**账本**：`shutil.move(tmp, path)` **原地重建覆盖** ——"
                                "与 `export_document` **同一族**（★ 计划里点的六个候选**漏了它**，"
                                "是这一轮扫出来的第七个）。按判定规则：同 `export_document`。"),
    "extract_archive": (False, "**误报**：`shutil.rmtree` 删的是它**自己刚解出来的**临时目录"
                               "（`except` 清理，`handler.py:613`）—— 不是用户的文件，"
                               "也不是「没要求的损失」（那次解压就是调用者的要求）。"),
    # 框架级（`bus/registry.py::_EXPLICIT_DESTRUCTIVE`）
    # 它们也在这张账本里，虽然本脚本 `--apply` 不碰框架（那一格走代码表）——
    #   理由：账本只许有一处。把 `create_skill` 的豁免只写在
    #   `_EXPLICIT_DESTRUCTIVE` 的行内注释里时，第 5 步那把尺子会把它抓出来
    #   （"命中却既没声明 true、也不在账本里"）—— 抓得对：**分散两处的豁免
    #   等于没有豁免**，而漏掉的那一处不会红。
    "create_skill": (False, "**误报**：`shutil.rmtree` 删的是它**自己的** `_wip_` 临时目录"
                            "（`core/skill_factory.py`，体检不过或搬完之后清理）—— "
                            "★ 不变量是**先体检、后落盘**，失败的现场 `skills/` 下一个字节都没多。"),
}
#
# 设计指针（上面三条 `False` 的账本同指这一处）：
#   · `bus/registry.py::_EXPLICIT_DESTRUCTIVE` 的 docstring —— 同一条判定规则的正式住处。
#   这三条进账本不是因为"以后有备份就不算破坏"
#      （备份不改写历史，手动改的文件它照样盖不住）。理由只有上面那条判定规则。

_INDENT = "    "

#: 工具条目那一行 —— 两种排版都认：本仓的 manifest 有两种来源，
#: 手写的是 `- name: <tool>` 打头，`yaml.safe_dump` 产出的是键排序
#: （`description` / `inputSchema` / `name` / `permission`…，`name` 在中间）。
#: 顶层那个 `name: <包名>` 也匹配这个正则 —— 它靠 `tool in want` 被排除。
_TOOL_LINE = re.compile(r"^(?:-\s*)?name:\s*(\S+)\s*$")


def _owner_pkg(owner: str | None) -> str | None:
    if owner and owner.startswith("skill:"):
        return owner.split(":", 1)[1]
    return None


def collect():
    """跑一趟装载，给每个工具算出 `(名字, 归属, 提议, 级, 证据)`。"""
    from scripts.measure_request import boot_registry
    boot_registry()

    import bus.registry as R

    index = _collect_defs()
    rows = []
    for name in sorted(R.list_tools()):
        owner = R.get_tool_owner(name)
        pkg = _owner_pkg(owner)
        if pkg is None:
            rows.append((name, owner, None, "framework", "框架/MCP —— 走代码里的表"))
            continue
        hits = [t for t in index.get(name, []) if t[0].startswith("skills/%s/" % pkg)]
        if len(hits) != 1:
            rows.append((name, owner, None, "?",
                         "同包里有 %d 个同名 def: %s"
                         % (len(hits), ["%s:%d" % (h[0], h[1]) for h in hits])))
            continue
        rel, lineno, tree, _src = hits[0]
        node = None
        for sub in ast.walk(tree):
            if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and sub.name == name and sub.lineno == lineno):
                node = sub
                break
        marks = _write_marks(node, tree, attrs=IRREVERSIBLE_ATTRS,
                             extra_rules=False) if node is not None else set()
        where = "%s:%d" % (rel, lineno)
        if marks:
            rows.append((name, owner, None, "A", where + " 不可逆 " + str(sorted(marks))))
        else:
            rows.append((name, owner, False, "B", where + " 无不可逆原语"))
    return rows


def report(rows) -> None:
    skill_rows = [r for r in rows if _owner_pkg(r[1])]
    buckets: dict[str, list] = {}
    for r in skill_rows:
        buckets.setdefault(r[3], []).append(r)
    print("=" * 72)
    print("注册工具 %d 个：技能 %d · 框架/MCP %d"
          % (len(rows), len(skill_rows), len(rows) - len(skill_rows)))
    print("=" * 72)
    print("A（AST 命中不可逆原语）-> ★ 待定 : %d" % len(buckets.get("A", [])))
    print("B（AST 干净）            -> false : %d" % len(buckets.get("B", [])))
    for k in ("?",):
        if buckets.get(k):
            print("%s（判不了）: %d" % (k, len(buckets[k])))
    for key, title in (("A", "★★ A 级 —— 要人拿主意的（AST 说它不可逆）"),
                       ("?", "[!] 判不了的")):
        if not buckets.get(key):
            continue
        print()
        print("-" * 72)
        print(title)
        print("-" * 72)
        for name, owner, _p, _lv, why in buckets[key]:
            decided = LEDGER.get(name)
            mark = ("-> %s  %s" % (decided[0], decided[1][:60]) if decided
                    else "-> <== 待定（去 LEDGER 里填，要带理由）")
            print("  %-20s %-22s %s" % (name, owner, why))
            print("  %-20s %s" % ("", mark))
    print()
    print("-" * 72)
    print("框架/MCP（走 `bus/registry.py::_EXPLICIT_DESTRUCTIVE`，本脚本不碰）")
    print("-" * 72)
    for name, owner, _p, _lv, _why in rows:
        if _owner_pkg(owner) is None:
            print("  %-24s %s" % (name, owner))


def apply(rows) -> int:
    """行级插入。A 级没答案就拒跑（不许把没做的决定糊过去）。"""
    pending = [r[0] for r in rows if r[3] == "A" and r[0] not in LEDGER]
    if pending:
        print("[!] 拒跑：还有 %d 个 A 级没答案 -> %s" % (len(pending), pending))
        print("    去本文件的 `LEDGER` 里填（**要带理由**）。")
        return 2

    changed = 0
    for pkg in sorted({_owner_pkg(r[1]) for r in rows if _owner_pkg(r[1])}):
        path = ROOT / "skills" / pkg / "manifest.yaml"
        raw = path.read_bytes().decode("utf-8")
        eol = "\r\n" if "\r\n" in raw else "\n"
        lines = raw.split(eol)
        want = {r[0]: (LEDGER[r[0]][0] if r[0] in LEDGER else r[2])
                for r in rows if _owner_pkg(r[1]) == pkg}

        # ① 定位插入点：插在这个工具自己那条 `permission:` 之后
        # （没有 `permission:` 就退回插在 `name:` 那一行之后）
        #
        # 不能只看 `lines[i + 1]`：有些 skill 的 manifest 是键排序的
        # （`yaml.safe_dump` 写的 —— `- description` / `inputSchema` / `name` /
        # `permission` 各占一行，按字母排），`permission:` 在 `name:` 之后好几行。
        # 只看下一行，那个包整包漏掉，而它的报错是
        # "manifest 里没有这些工具" —— 看起来像 manifest 坏了，
        # 不像"我的行识别器只认一种排版"。
        #
        # 而且不许移动 `permission:` 那一行（上面那个写法会把键排序的文件
        # 弄乱序），先定位、再输出，两步走。
        anchor: dict[str, int] = {}
        cur = None
        for i, s in enumerate(x.strip() for x in lines):
            m = _TOOL_LINE.match(s)
            if m:
                name = m.group(1)
                cur = name if name in want else None
                if cur and cur not in anchor:
                    anchor[cur] = i            # 兜底：`name:` 那一行之后
                continue
            if s.startswith("- "):
                cur = None                     # 进了下一个列表项（不是我们要的工具）
                continue
            if cur and s.startswith("permission:"):
                anchor[cur] = i                # 优先：`permission:` 那一行之后
        seen = set(anchor)

        after: dict[int, str] = {}
        for tool, idx in anchor.items():
            nxt = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
            if nxt.startswith("destructive:"):
                continue                       # 幂等：已经有就不重复插
            after[idx] = tool

        # ② 按行输出，在标记过的那一行后面插入
        #
        # 缩进必须跟锚点那一行走，不能写死：本仓的 manifest 有两种来源，
        # 手写的块缩进 4 空格，`yaml.safe_dump` 产出的块缩进 2 空格。
        # 写死 4，在键排序那份上产出 `  permission: read` / `    destructive: false`
        # —— YAML 直接解析错（`destructive` 成了 `read` 的嵌套键），
        # 而它长得只像缩进不好看。
        out = []
        for i, line in enumerate(lines):
            out.append(line)
            if i in after:
                changed += 1
                lead = line[:len(line) - len(line.lstrip())]
                if line.lstrip().startswith("- "):
                    lead += "  "               # `- name:` 打头时，兄弟键再缩两格
                out.append("%sdestructive: %s"
                           % (lead, "true" if want[after[i]] else "false"))

        missing = set(want) - seen
        if missing:
            print("[!] %s: manifest 里没有这些工具 -> %s" % (pkg, sorted(missing)))
            continue
        path.write_bytes(eol.join(out).encode("utf-8"))
        print("  [ok] skills/%s/manifest.yaml  +%d 行" % (pkg, len(want)))
    print("总插入 %d 行" % changed)
    return 0


def main(argv: list[str]) -> int:
    do_apply = "--apply" in argv
    rows = collect()
    report(rows)
    if do_apply:
        print()
        return apply(rows)
    print()
    print("（只读模式 —— 加 --apply 才动文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
