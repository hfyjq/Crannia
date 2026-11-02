"""只读生成器 —— 提议每个工具的 `permission:`。

## 它是干什么的，以及它不干什么

它按证据给每个技能工具提一个档，不按今天的现状抄一遍：

| 级 | 判据 | 提议 | 为什么这不是"搬家" |
|---|---|---|---|
| A | AST 有写标记（`_write_marks` 非空） | `write` | 证据来自 `tests/test_permission_tiers.py::_write_marks`，与 `_classify_permission` 零共享代码 |
| B | AST 干净 且 今天已是 `read` | `read` | 该工具一直是 `read`，且那条主不变量长期钉着；AST 现在只是复核 |
| C | AST 干净 且 今天不是 `read` | 留空 | 唯一真要动脑的地方 |

C 级留空是刻意的，也是这一步真正的交付物。
`admin` 这个档名在撒谎 —— 它不表示"危险"，它表示"分类器没认出来"；
而 C 级恰好就是那批"认不出来"的。**让生成器把 `_classify_permission` 的现值写进文件，
等于把那个猜抄进一份看起来像声明的东西里** —— 抄一遍不算做过决定。

## 用法

```bash
.venv/Scripts/python.exe scripts/propose_permissions.py          # 只报，一个字都不写
.venv/Scripts/python.exe scripts/propose_permissions.py --apply  # C 级全部有答案之后才允许
```

`--apply` 是行级插入（在 `  - name: <tool>` 那一行后面插一行 `    permission: ...`），
不是 `yaml.safe_dump` 回写 —— 后者会把 manifest 里手写的注释、
`>` 折行块、字段顺序全部重排。手工写的东西不能被机器推平。

manifest 是 CRLF（`core/file_tools.py` 那条纪律记着本仓是混排的），
所以插入的那一行也必须用 `\r\n`。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 复用那条现成的 AST 仪器 —— 不抄第二份。
# 抄第二份的代价不是多写 50 行，是两份会漂移；
# 而这一份里的每一条剔除都是"先红了才剔的"结果，抄丢了没有任何东西会提醒你。
from tests.test_permission_tiers import _collect_defs, _write_marks  # noqa: E402

#: C 级的答案 —— 空着就说明还没决定。填进去必须带理由。
#:
#: 格式：`工具名: (档位, "为什么")`。填完 `--apply` 才会动文件。
C_LEVEL_DECISIONS: dict[str, tuple[str, str]] = {
    "mark_done": ("write", "★ 调 store.mark_status(...) —— **`mark_status` 不在 `_WRITE_ATTRS` 里**，"
                           "所以 AST 判它干净；这正是 C 级要人看的理由"),
    # file_parser
    "deliver_file": ("write", "调 _record_delivery(...) 记交付台账 + mail_handler.send_email(...) 发信；"
                              "今天 admin -> write（**行为等价**，只是标签不再撒谎）"),
    "request_cross_zone_search": ("admin", "★ R13：本轮**不动闸**。它本身只搜（wm.search_files）+ 挂起审批，"
                                           "但它在 `_APPROVAL_CAPABLE_TOOLS` 里、而且扫的是用户全盘 —— "
                                           "把它提成 read 是一次**真闸改动**，单独立项"),
    # mail_parser
    "send_email": ("write", "调 server.sendmail(...) 真发信 —— **不可逆**。"
                            "★ `_WRITE_ATTRS` 里**没有 send 族**（判据：delete/overwrite/send 三族），"
                            "所以 AST 也判它干净；这是 destructive 候选"),
    # office_parser
    "add_slide": ("write", "调 session.blocks.append(...) —— 改的是**共享会话态**，"
                           "与 `create_document` 同一族（从 read 里剔出来的那条）"),
    "set_slide_design": ("write", "调 session.spec_lock.get/setdefault(...) —— 同上，改共享会话态"),
    "convert_to_pdf": ("write", "调 subprocess.run(...) 起 soffice + out_dir.mkdir(...) 落盘 —— 写文件"),
    "validate_formula_syntax": ("write", "★ 它**其实真读**（load_workbook 打开校验公式，wb.close 不存盘），"
                                         "但 R13：**本轮不提成 read** —— 任何「提成 read」都是一次真闸改动，"
                                         "夹在迁移里就查不出是哪次改的。单独立项"),
}

_INDENT = "    "


def _owner_pkg(owner: str | None) -> str | None:
    if owner and owner.startswith("skill:"):
        return owner.split(":", 1)[1]
    return None


def collect():
    """跑一趟装载，给每个工具算出 `(归属, 今天, 提议, 来源, 证据)`。"""
    from scripts.measure_request import boot_registry
    boot_registry()

    import bus.registry as R

    index = _collect_defs()
    rows = []
    for name in sorted(R.list_tools()):
        owner = R.get_tool_owner(name)
        today = R.get_tool_permission(name)
        pkg = _owner_pkg(owner)

        if pkg is None:
            rows.append((name, owner, today, None, "framework", "框架/MCP —— 走代码里的表"))
            continue

        hits = [t for t in index.get(name, []) if t[0].startswith(f"skills/{pkg}/")]
        if not hits:
            rows.append((name, owner, today, None, "?", "源码里找不到同名 def"))
            continue
        if len(hits) > 1:
            rows.append((name, owner, today, None, "?",
                         "同包里有 %d 个同名 def: %s"
                         % (len(hits), [f"{h[0]}:{h[1]}" for h in hits])))
            continue

        rel, lineno, tree, _src = hits[0]
        node = None
        for sub in ast.walk(tree):
            if (isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and sub.name == name and sub.lineno == lineno):
                node = sub
                break
        marks = _write_marks(node, tree) if node is not None else set()
        where = f"{rel}:{lineno}"

        if marks:
            rows.append((name, owner, today, "write", "A", where + " 写标记 " + str(sorted(marks))))
        elif today == "read":
            rows.append((name, owner, today, "read", "B", where + " AST 干净且今天已是 read"))
        else:
            rows.append((name, owner, today, None, "C", where + " AST 干净，但今天不是 read"))
    return rows


def reserved_vs_explicit():
    """`_EXPLICIT_PERMS` 该剪成什么样 —— 两个集合的差。"""
    import bus.registry as R
    explicit = set(R._EXPLICIT_PERMS)
    reserved = R.reserved_tool_names()
    skill_owned = {n for n in explicit
                   if _owner_pkg(R.get_tool_owner(n)) is not None}
    return explicit, reserved, skill_owned


def report(rows) -> None:
    skill_rows = [r for r in rows if r[1] and r[1].startswith("skill:")]
    buckets: dict[str, list] = {}
    for r in skill_rows:
        buckets.setdefault(r[4], []).append(r)

    print("=" * 72)
    print("注册工具 %d 个：技能 %d · 框架/MCP %d"
          % (len(rows), len(skill_rows), len(rows) - len(skill_rows)))
    print("=" * 72)
    print("A（AST 有写标记）-> write : %d" % len(buckets.get("A", [])))
    print("B（AST 干净且已是 read）-> read : %d" % len(buckets.get("B", [])))
    print("C（AST 干净但不是 read）-> ★ 待定 : %d" % len(buckets.get("C", [])))
    for k in ("?",):
        if buckets.get(k):
            print("%s（判不了）: %d" % (k, len(buckets[k])))

    for key, title in (("C", "★★ C 级 —— 要人拿主意的（AST 说它不写，但今天不是 read）"),
                       ("?", "[!] 判不了的")):
        if not buckets.get(key):
            continue
        print()
        print("-" * 72)
        print(title)
        print("-" * 72)
        for name, owner, today, _prop, _lv, why in buckets[key]:
            print("  %-28s 归属 %-24s 今天 %-6s %s" % (name, owner, today, why))

    print()
    print("-" * 72)
    print("_EXPLICIT_PERMS 剪法")
    print("-" * 72)
    explicit, reserved, skill_owned = reserved_vs_explicit()
    print("  今天 %d 条；其中 %d 条是**技能自己的工具名**（要删 —— 它们搬进 manifest）"
          % (len(explicit), len(skill_owned)))
    print("  要保留/补上的框架与 MCP 名：%d 条 -> %s"
          % (len(reserved), sorted(reserved)))
    print("  剪完应为 %d 条" % len(reserved))


def emit_blocks(rows) -> None:
    """按包打印可以直接粘进 manifest 的 `permission:` 行。"""
    by_pkg: dict[str, list] = {}
    for name, owner, today, prop, level, why in rows:
        pkg = _owner_pkg(owner)
        if pkg is None:
            continue
        by_pkg.setdefault(pkg, []).append((name, today, prop, level))

    for pkg in sorted(by_pkg):
        print()
        print("=" * 72)
        print("skills/%s/manifest.yaml" % pkg)
        print("=" * 72)
        for name, today, prop, level in sorted(by_pkg[pkg]):
            if prop is None:
                print("  %-28s permission:   <== 待定（今天 %s，%s 级）" % (name, today, level))
            else:
                print("  %-28s permission: %s" % (name, prop))


def apply(rows) -> int:
    """行级插入。C 级没答案就拒跑（不许把没做的决定糊过去）。"""
    pending = [r[0] for r in rows
               if r[4] == "C" and r[0] not in C_LEVEL_DECISIONS]
    if pending:
        print("[!] 拒跑：还有 %d 个 C 级没答案 -> %s" % (len(pending), pending))
        print("    去本文件的 C_LEVEL_DECISIONS 里填（**要带理由**）。")
        return 2

    changed = 0
    for pkg in sorted({_owner_pkg(r[1]) for r in rows if _owner_pkg(r[1])}):
        path = ROOT / "skills" / pkg / "manifest.yaml"
        raw = path.read_bytes().decode("utf-8")
        crlf = "\r\n" in raw
        eol = "\r\n" if crlf else "\n"
        lines = raw.split(eol)

        want = {}
        for name, owner, today, prop, level, _why in rows:
            if _owner_pkg(owner) != pkg:
                continue
            want[name] = prop if prop is not None else C_LEVEL_DECISIONS[name][0]

        out, seen = [], set()
        for i, line in enumerate(lines):
            out.append(line)
            stripped = line.strip()
            if stripped.startswith("- name:"):
                tool = stripped.split(":", 1)[1].strip()
                if tool in want and tool not in seen:
                    seen.add(tool)
                    # 已经有 permission 就不重复插
                    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    if not nxt.startswith("permission:"):
                        out.append("%spermission: %s" % (_INDENT, want[tool]))
                        changed += 1

        missing = set(want) - seen
        if missing:
            print("[!] %s: manifest 里没有这些工具 -> %s" % (pkg, sorted(missing)))
            continue
        path.write_bytes(eol.join(out).encode("utf-8"))
        print("  [ok] %s  +%d 行" % (path.relative_to(ROOT), len(want)))
    print("总插入 %d 行" % changed)
    return 0


def main(argv: list[str]) -> int:
    do_apply = "--apply" in argv
    rows = collect()
    report(rows)
    emit_blocks(rows)
    if do_apply:
        print()
        return apply(rows)
    print()
    print("（只读模式 —— 加 --apply 才动文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
