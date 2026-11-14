"""Wire 上的固定开销：只放「每一轮都有用」的东西。

判据：全量重发的判据不是"有用吗"，是"每一轮都有用吗"。
system prompt 与工具表正是全量重发的那两格。

仪器（`scripts/measure_request.py`，同一个仪器量前后）：

| | 之前 | 之后 |
|---|---|---|
| 工具表（14 个常驻） | 3,408 | 2,837 |
| 固定开销合计 | 5,520 | 4,949 |

拆成两半：`$schema` 剥离 -266/请求 · 四段 description 压缩 -305/请求。

一条读数的陷阱：`measure_request.py` 的"留一法"逐工具边际在这两批改动前后
不可比 —— 整张工具表是一次 `json.dumps` 序列化的，别的工具变短会让分词边界
移动（`list_workspace_files` 没被碰过，边际却从 271 漂到 252）。
可靠的只有总数。

五条断言：
  1. wire 上的工具 schema 一律不带 `$schema`
  2. 装载期的自检仍要拦未知方言 —— 别把检查一起删了（那才是真的损失）
  3. 文件条目只有四个字段，推得出来的三个不许回来
  4. 一页装得下预算：32 条 < 6,000 字符（工具结果截断线）
  5. 类型映射只有一处 —— 不要恢复成 3 份（模块级 13 条 + 两处内联 7 条）

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_wire_budget.py

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。
"""

import ast
import contextlib
import json
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
_SCRIPTS = _PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_HANDLER = _PROJECT_ROOT / "skills" / "file_parser" / "handler.py"

#: 工具结果的截断线（`config.yaml: tool_result_max_chars`）。越过它就是
#: 在一条 JSON 中途截断 —— 模型拿到的是坏 JSON，而它看起来像"工具坏了"。
_TOOL_RESULT_BUDGET = 6000


def _boot():
    """复用 `measure_request.boot_registry()` —— 不抄第二份装载逻辑。"""
    from measure_request import boot_registry

    boot_registry()
    import bus.registry as R

    return R


# ------------------------------------------------------------------
# 1 + 2. `$schema` 从 wire 上挪走，但自检留在装载期
# ------------------------------------------------------------------

def test_no_tool_schema_carries_dollar_schema_on_the_wire():
    """`$schema` 是 99 个工具里逐字相同的一个常量 URI（实测 19 tok x 14 个）。

    它的语义是"声明本 schema 用哪个方言"，不是给校验器读的数据；而收它的
    那一侧（function calling 的 `parameters`）只是一个 JSON Schema 对象，
    OpenAI 兼容端点不按 meta-schema 校验它。
    """
    R = _boot()
    tools = R.get_tool_schemas()
    assert tools, "registry empty -- boot failed"

    offenders = []
    for t in tools:
        name = (t.get("function") or {}).get("name") or t.get("name") or "?"
        # 两种形状都查：`function.parameters`（生产）与裸 `parameters`（早期）
        for cand in ((t.get("function") or {}).get("parameters"),
                     t.get("parameters")):
            if isinstance(cand, dict) and "$schema" in cand:
                offenders.append(name)

    assert not offenders, (
        "$schema back on the wire for: %r -- 266 tok/request, zero benefit"
        % (offenders[:5],)
    )
    print("  [ok] %d 个工具 schema 都不带 $schema" % len(tools))


def test_the_load_time_check_still_rejects_an_unknown_dialect():
    """上一条的伴生断言：挪走的是 token，不是自检。

    没有这一条，把 `validate_tool_schema` 里那段整个删掉，上面那条照样绿
    —— 而那时我们是真的丢了一个检查（别把"挪了位置"和"删了"混起来）。
    """
    from bus.loaders.skill_loader import validate_tool_schema

    ok = {"$schema": "https://json-schema.org/draft/2020-12/schema",
          "type": "object", "properties": {}}
    validate_tool_schema(ok)  # 认得的方言：放行

    bad = dict(ok)
    bad["$schema"] = "https://example.com/not-a-dialect"
    try:
        validate_tool_schema(bad)
    except ValueError:
        print("  [ok] 装载期仍然拦得住未知的 $schema 方言")
        return
    raise AssertionError(
        "validate_tool_schema 放行了一个未知方言 —— 自检被删了，"
        "而第 1 条断言会照绿"
    )


# ------------------------------------------------------------------
# 3 + 4. 文件条目：形状与预算
# ------------------------------------------------------------------

def test_file_entry_keeps_only_the_four_non_derivable_fields():
    """条目只留四个字段，其余都推得出来。

    （`workspace/` 的 18 个真实文件）：216 -> 153 字符/条。
    砍掉的三个各有"推得出来"的理由：
      * `name` = `path` 的最后一段
      * `size` = `size_human` 的机器读法（模型不排序）
      * `type` = `ext` 的中文标签，而且有损 —— `Word文档` 分不出
        `.doc` / `.docx`（下游是两个不同的工具），`.jpg` / `.png` 恒为空串
    """
    from skills.file_parser.handler import _file_entry

    e = _file_entry("D:/ws/a.docx", ".docx", 55808, "2025-11-23T11:51:42")
    assert set(e) == {"path", "ext", "size_human", "modified"}, (
        "字段集变了：%r -- 回来的每一个都要问一遍'它每一轮都有用吗'" % (sorted(e),)
    )
    assert e["size_human"] == "54.5 KB", e
    # ISO 归一，与 legacy 分支的 `%Y-%m-%d %H:%M` 对齐
    assert e["modified"] == "2025-11-23 11:51", e

    # 缺省路径（跨区缓存不扫盘，只有路径）不许多塞字段
    slim = _file_entry("D:/ws/a.docx", ".docx")
    assert set(slim) == {"path", "ext"}, slim
    print("  [ok] 条目字段集 = path/ext/size_human/modified，缺省时不补空值")


def test_a_full_index_page_fits_the_tool_result_budget():
    """36 条 < 6,000 字符。

    用的路径前缀与文件名取自本机真实的工作区，不是构造的最大值 ——
    教训："可不可能"与"会不会"是两个断言，构造最大值只能证明前者。

    为什么是 36：生产同源量得（`data/settings.json` + 真 `WorkspaceManager`）
    索引 34 条 = 5,449 字符（约 160 字符/条）。这条断言要跟着真实规模走，
    而不是抄一个更小的数显得安全 —— 否则它测的是一个不存在的场景。

    天花板约 37 条（6,000 / 160）—— 越过它就会被中途截断。
    索引继续长（工作组再加文件夹）时这条会红，而那正是它该做的事。
    """
    from skills.file_parser.handler import _file_entry

    root = str(_PROJECT_ROOT / "workspace")
    names = [
        "2026年5月核心投资机会与资产配置策略研究报告.pdf",
        "2025443267_刘书宇_大作业.doc",
        "2026年5月核心投资机会_核心标的整理.docx",
        "总经理办公会议案表.docx",
    ]
    n_entries = 36
    entries = []
    for i in range(n_entries):
        n = names[i % len(names)]
        entries.append(_file_entry(
            root + "\\" + n, Path(n).suffix.lower(), 55808, "2025-11-23 11:51",
        ))
    blob = json.dumps(entries, ensure_ascii=False)
    assert len(blob) < _TOOL_RESULT_BUDGET, (
        "%d 条 = %d 字符，越过 %d 的截断线 -- 中途截断给模型的是坏 JSON"
        % (n_entries, len(blob), _TOOL_RESULT_BUDGET)
    )
    print("  [ok] %d 条 = %d 字符（预算 %d；生产实测 34 条 = 5,449）"
          % (n_entries, len(blob), _TOOL_RESULT_BUDGET))


# ------------------------------------------------------------------
# 5. 单一来源（防倒退）
# ------------------------------------------------------------------

def test_type_map_and_size_formatter_live_in_exactly_one_place():
    """类型映射曾三份、size 格式化曾三份，且两份 size 写法不同。

    坏形状（两份名单各写各的、漂移了）：
      * `_FILE_TYPE_MAP`（模块级 13 条）+ 两处内联 dict（各 7 条）
      * `f"{n} B"`（带空格）与 `f"{n}B"`（不带）—— 同一个量两个格式，
        而且不会报错（各算各的比算错更危险）

    用 AST 走字典字面量，不用子串匹配 —— `_file_entry()` 的 docstring 里
    就写着 `Word文档`（讲它为什么有损），子串检查会误报。
    """
    src = _HANDLER.read_text(encoding="utf-8")
    tree = ast.parse(src)

    type_maps = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if ".docx" in keys or ".pdf" in keys:
            type_maps.append(node.lineno)

    assert not type_maps, (
        "handler.py:%r 又出现了扩展名 -> 中文的字典字面量 —— "
        "类型映射只许有一处（`_file_entry` 的 docstring 记着为什么砍掉它）"
        % (type_maps,)
    )

    n_size_human = src.count('"size_human"')
    assert n_size_human == 1, (
        '"size_human" 出现 %d 次，应为 1（只有 `_file_entry` 那一处）'
        % n_size_human
    )
    print("  [ok] 类型映射 0 份、size_human 字段 1 处、size 格式化 1 处")


# ------------------------------------------------------------------

@contextlib.contextmanager
def _temp_tool_results():
    """把 `data/workdata/tool_results/` 指到临时目录。

    为什么必须：`_enforce_message_tool_budget` 走的是 `_truncate_to_artifact`，
    而它会真落盘。指到临时目录是内建的：
    「测试自己进临时目录」，不是"记得清" —— 不指过去就会把文件写进生产目录。
    系统临时目录必须在 chdir 之前建好。
    """
    from core import agent_loop as AL
    saved = AL._TOOL_RESULTS_DIR
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        AL._TOOL_RESULTS_DIR = Path(td) / "tool_results"
        try:
            yield AL._TOOL_RESULTS_DIR
        finally:
            AL._TOOL_RESULTS_DIR = saved


def test_message_budget_untouched_below_the_line():
    """没超线就一个字节都不动（单条消息内 tool_result 总量闸）。

    与单条闸的关系：单条闸（`tool_result_max_chars`）管每一条，
    这条管一批合起来 —— 一轮 4 个调用各自都在单条闸以内，合起来照样 4 倍。
    """
    from core.agent_loop import _enforce_message_tool_budget
    msgs = [{"role": "assistant", "content": ""}] + [
        {"role": "tool", "tool_call_id": "c%d" % i, "content": "x" * 1000}
        for i in range(3)]
    before = [dict(m) for m in msgs]
    n, saved = _enforce_message_tool_budget(msgs, 1, [])
    assert (n, saved) == (0, 0), (n, saved)
    assert msgs == before, "没超线却动了消息"
    print("   [ok] 未超线 -> 0 条被压")


def test_message_budget_shrinks_the_largest_first_and_keeps_small_ones():
    """超线时：从最大的那条开始压，小结果原样保留 —— 均分是错的。

    断言的是总量降到线内 + 小的那条没被动 + 被压的那条带可读指针。
    只看"函数返回了非零"等于没测（那正是"绿了但没测到"）。
    """
    from core.agent_loop import (_enforce_message_tool_budget,
                                 _MESSAGE_TOOL_RESULT_BUDGET as BUDGET)
    big = "A" * (BUDGET + 10_000)      # 必须真的超线
    small = "S" * 300
    msgs = [
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": "c0", "content": small},
        {"role": "tool", "tool_call_id": "c1", "content": big},
    ]
    with _temp_tool_results():
        n, saved = _enforce_message_tool_budget(msgs, 1, [])
    total = sum(len(m["content"]) for m in msgs if m["role"] == "tool")
    assert total <= BUDGET, "压完还是超线：%d > %d" % (total, BUDGET)
    assert msgs[1]["content"] == small, "**小结果被动了** —— 均分才是那个错做法"
    assert "<persistent_output" in msgs[2]["content"], \
        "被压的那条没有可读指针（指针必须真能取到）"
    assert saved > 0
    print("   [ok] 压最大的一条：总量 %d -> %d，小结果原样，指针可读"
          % (len(big), total))


def test_message_budget_does_not_change_the_message_shape():
    """只换 `content`，不加字段 —— 加字段就上 wire，动的是前缀缓存。

    工具名是从 `tool_calls` 按 id 反查的，不是存在消息里。
    """
    from core.agent_loop import (_enforce_message_tool_budget,
                                 _MESSAGE_TOOL_RESULT_BUDGET as BUDGET)
    msgs = [
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": "c0", "content": "B" * (BUDGET + 5_000)},
    ]
    keys_before = set(msgs[1])
    with _temp_tool_results():
        _enforce_message_tool_budget(
            msgs, 1, [{"id": "c0", "function": {"name": "read_excel_data"}}])
    assert set(msgs[1]) == keys_before, (
        "消息形状变了（多了 %r）—— 那会改 wire 字节" % (set(msgs[1]) - keys_before))
    print("   [ok] 只换 content，键集不变（不上 wire 的改动）")


def test_message_budget_is_failsafe_when_everything_is_small():
    """fail-safe：一批都很小（但条数多）时不许乱压 —— 压它们省不下还多一堆指针。"""
    from core.agent_loop import _enforce_message_tool_budget
    msgs = [{"role": "assistant", "content": ""}] + [
        {"role": "tool", "tool_call_id": "c%d" % i, "content": "x" * 1500}
        for i in range(50)]
    before = [dict(m) for m in msgs]
    n, _ = _enforce_message_tool_budget(msgs, 1, [])
    assert n == 0, "全是小结果（每条 <= 下限）却压了 %d 条" % n
    assert msgs == before
    print("   [ok] 全小结果 -> 一条都不压（fail-safe）")


def test_message_budget_ignores_non_tool_messages():
    from core.agent_loop import _enforce_message_tool_budget
    msgs = [{"role": "user", "content": "Z" * 300_000},
            {"role": "assistant", "content": "Y" * 300_000}]
    before = [dict(m) for m in msgs]
    n, _ = _enforce_message_tool_budget(msgs, 0, [])
    assert n == 0 and msgs == before, "动了非 tool 消息"
    print("   [ok] 只碰 role=tool 的消息")


def _main():
    tests = [
        test_no_tool_schema_carries_dollar_schema_on_the_wire,
        test_the_load_time_check_still_rejects_an_unknown_dialect,
        test_file_entry_keeps_only_the_four_non_derivable_fields,
        test_a_full_index_page_fits_the_tool_result_budget,
        test_type_map_and_size_formatter_live_in_exactly_one_place,
        # 单条消息内的 tool_result 总量闸
        test_message_budget_untouched_below_the_line,
        test_message_budget_shrinks_the_largest_first_and_keeps_small_ones,
        test_message_budget_does_not_change_the_message_shape,
        test_message_budget_is_failsafe_when_everything_is_small,
        test_message_budget_ignores_non_tool_messages,
    ]
    failed = 0
    for t in tests:
        print("[RUN ] %s" % t.__name__)
        try:
            t()
        except Exception as exc:
            failed += 1
            print("FAIL  %s: %s: %s" % (t.__name__, type(exc).__name__, exc))
        else:
            print("PASS  %s" % t.__name__)
    print()
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
