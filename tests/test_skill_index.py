"""技能索引 + 常驻集收缩。

这一档是什么 —— 「索引常驻 + 正文按需拉」：常驻的工具 schema 换来一份
技能粒度的索引，具体工具用 `search_tools` 按需拉。

实测收益（`scripts/measure_request.py`，同一个仪器）：
    常驻工具 19 -> 12 · 工具表 4,486 -> 3,113 tokens · 固定开销 6,552 -> 5,179

这一档最大的风险 —— 把工具移出常驻 = 它们只能靠索引 + `search_tools` 找到。
真实失败形状是"写文件的工具被剔除了，模型想写文件却没有工具"。
所以下面每一条断言都在盯这个：

  * 索引必须覆盖每一个有工具的 skill（模型得知道有什么）
  * 被移出去的工具必须真能被搜到（名字查询 + 自然语言查询）
  * 索引不许进 messages / 不许进 system prompt

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_skill_index.py
"""
#@exclusive: skills-dir
# 这个文件会真的碰 `skills/`（建 / 删 / 扫），而别的测试会在那个目录里建又删（净变化为零），互相撞车。
# → 机制与理由在 `scripts/run_tests.py::_EXCLUSIVE_RE`。

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml  # noqa: E402

import bus.registry as R  # noqa: E402
from core.skill_index import (  # noqa: E402
    append_skill_index, build_skill_index, index_coverage, skill_index_stats,
)

#: 与 `scripts/measure_request.py` 同一套最小装载：不调 `init_bus`
#: （那会加载 MCP 与 IM 通道，真连网）。只在模块级做一次。
_BOOTED = {"done": False}


def _boot():
    """最小装载 —— 但必须包含 MCP 内置工具。

    不注册 MCP 会让可达性断言跳过 `web_search`/`web_fetch`，
    而那正是真正搜不到的两个。一条会自己跳过的护栏，等于没有护栏。

    `mcpx.servers.web_search.register_tools()` 只注册 schema，不联网，
    所以覆盖它们零成本。
    """
    if _BOOTED["done"]:
        return
    from bus.loaders.skill_loader import load_skill, load_skill_lazy

    R.register_search_tool()
    # 会话条件工具 `run_workflow` 也必须有 —— 否则
    #   "ultra 档可达"那条判据会因为夹具没装它而红，而不是因为行为错了。
    R.register_workflow_tool()
    for entry in sorted(Path("skills").iterdir()):
        mf = entry / "manifest.yaml"
        if not (entry.is_dir() and mf.exists()):
            continue
        m = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
        if m.get("draft"):
            continue
        if m.get("load_strategy", "eager") == "on_demand":
            load_skill_lazy(entry)
        else:
            load_skill(entry)
    try:  # MCP 内置（web_search / web_fetch）—— 生产里由 init_mcp 注册
        from mcpx.servers import web_search as _mcp_web
        _mcp_web.register_tools()
    except Exception as exc:
        print(f"  [!] MCP 内置工具未注册（{type(exc).__name__}）—— 覆盖面会缺一块")
    _BOOTED["done"] = True


def _skills_with_tools() -> dict:
    out = {}
    for entry in sorted(Path("skills").iterdir()):
        mf = entry / "manifest.yaml"
        if not (entry.is_dir() and mf.exists()):
            continue
        m = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
        if m.get("draft") or m.get("kind") != "skill":
            continue
        names = [t["name"] for t in (m.get("tools") or [])]
        if names:
            out[entry.name] = names
    return out


# ------------------------------------------------------------------
# 1. 索引的覆盖面 —— 模型得先知道"有什么"
# ------------------------------------------------------------------

def test_index_covers_every_skill_that_has_tools():
    """每个有工具的 skill 都必须在索引里出现。

    索引是"模型知道有什么能力"的唯一来源：漏一个 skill，它下面所有工具
    就等于不可达（模型搜不到它不知道存在的东西）。
    """
    _boot()
    index = build_skill_index()
    missing = [name for name in _skills_with_tools() if name not in index]
    assert not missing, f"索引漏了这些 skill（它们的工具会变成不可达）：{missing}"
    print(f"  [ok] 索引覆盖全部 {len(_skills_with_tools())} 个有工具的 skill")


def test_index_never_promises_tools_that_are_not_registered():
    """反方向：索引里承诺的 skill，注册表里必须真有能调的工具。

    这是那条联动不变式在我们这边的形态：索引 ⊆ 能调到的。
    （严格等号做不到 —— `get_tool_handler` 查全量注册表，没索引的也能调。）

    恒真断言防不胜防：for name in X: assert name in X
    跑起来永远是绿的，什么也没查。所以这里查注册表
    （清单里有、但注册失败的工具体现不出来），并断言"循环确实走遍了所有 skill"，
    防止它退化成又一次空转。
    """
    _boot()
    index = build_skill_index()
    registered = set(R.list_tools())
    skills = _skills_with_tools()

    promised, broken = [], []
    for skill, names in skills.items():
        if f"- {skill} (" not in index:
            continue
        promised.append(skill)
        if not any(n in registered for n in names):
            broken.append((skill, names[:3]))

    # 非空性守卫：循环必须真的走过每一个 skill，否则这条测试又是空转
    assert len(promised) >= 1, "索引里一个 skill 都没匹配上 —— 这条测试空转了"
    assert not broken, (
        "索引承诺了工具、但注册表里一个都没有（模型会调不到）："
        + "\n  ".join(f"{s}: 清单里写着 {n}" for s, n in broken)
    )
    print(f"  [ok] 索引承诺的 {len(promised)} 个 skill，注册表里都真有可调工具")


def test_index_is_generated_not_hardcoded():
    """索引必须从清单现算 —— 手写的目录会与注册表漂移。

    做法：临时多造一个 skill 目录，看索引是否立刻反映它。
    （手写常量做不到这一点。）
    """
    _boot()
    import tempfile

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        d = Path(tmp) / "zz_probe_skill"
        d.mkdir()
        (d / "manifest.yaml").write_text(
            "kind: skill\nname: zz_probe_skill\n"
            "description: 探测用\nload_strategy: on_demand\n"
            "tools:\n  - name: zz_probe_tool\n    description: probe\n"
            "    inputSchema: {type: object, properties: {}}\n",
            encoding="utf-8",
        )
        index = build_skill_index(tmp)
        assert "zz_probe_skill" in index, index
        assert "1 个工具" in index, index
    print("  [ok] 索引随清单现算（临时加一个 skill 立刻反映）")


# ------------------------------------------------------------------
# 2. 被移出常驻的工具必须仍然找得到
# ------------------------------------------------------------------

#: 从 `_CORE_TOOLS` 移出去、且用户在真实任务里可能需要的工具，
#: 每条配一个"用户会怎么说"的自然语言查询。名字查询谁都能中，
#: 自然语言这一列才是真考验。
#:
#: `web_search`/`web_fetch` 曾在这一列里，现在是常驻的 ——
#: 它们的中文检索面太薄（只有"正在联网搜索"一句），
#: 口语化的"上网搜一下"搜不到。常驻 = 不依赖检索。
_MOVED_MUST_BE_FINDABLE = [
    ("parse_pdf", "读一下这个 pdf"),
    ("parse_csv", "解析 csv 表格"),
    ("split_pdf", "把这个 pdf 拆开"),
    ("merge_pdfs", "把几个 pdf 合成一个"),
    ("add_slide", "加一页幻灯片"),
    ("export_document", "把文档保存成文件"),
    ("extract_archive", "解压这个压缩包"),
    ("copy_to_workspace", "把文件复制到工作区"),
]

#: 同一个工具的多条真实话术。
#:
#: 为什么不并进上面那张表：那张表的口径是「一个工具一条话术」，
#: 并进去会让它那句"N 个移出的工具都能被自然语言搜到"变成谎话
#: （同一个工具被数 8 次，而工具其实只有 9 个）。
#:
#: 这张表是用一次真失败换来的：把 `format_excel_range` 的那一条
#: 原话修好了（「A 到 Y 列居中」93 → 5），而同一能力换个说法就又掉出去：
#: `把这一列居中` 名次 8 · `把列宽调大一点` 名次 38
#: （`search_tools` 默认 `limit=5`，不可达）。
#:判据：修完要拿一组同义话术量，不是只量那一句 ——
#: 否则修的是"那句话"，不是"那个能力"。
_REACH_PHRASINGS = [
    ("format_excel_range", "A 到 Y 列居中"),
    ("format_excel_range", "把这一列居中"),
    ("format_excel_range", "把列宽调大一点"),      # 名次 38 → 1
    ("format_excel_range", "把这一列调窄"),
    ("format_excel_range", "字号调大一点"),
    ("format_excel_range", "把这行调高一些"),
    ("format_excel_range", "给这列加个边框"),
    ("format_excel_range", "设置单元格底色"),
    ("plot_excel_data",    "画一张价格走势图"),
    ("remove_excel_duplicates", "去掉这张表里重复的行"),
    ("filter_excel_data",  "把销售额大于一万的筛出来"),
    ("freeze_excel_panes", "冻结首行"),
]

#: 负对照：不该命中的那一条。它证明上面那张表不是恒真
#: （一个"什么都能过"的断言等于没有断言）。
_REACH_NEGATIVE = ("format_excel_range", "把这个 pdf 拆开")


def test_every_tool_is_either_resident_or_findable():
    """总不变量：每一个注册的工具，要么常驻，要么按名字搜得到。

    只钉"用户可能用到"的那几个会恰好漏掉真正坏掉的（`web_search`/`web_fetch`：
    移出常驻、按名字也搜不到，而且索引里连提都没提，完全不可达）。

    名字查询是最弱的检索（名字命中权重 +8/+12，几乎必中）——
    所以这一条只用来抓"彻底不可达"，不保证"意图能搜到"（那是上面那条的活）。

    会话条件工具（`_SESSION_TOOLS`）不在这条里 ——
    它们按设计对拿不到它们的会话搜不出来（`search_tools` 与 `note_discovered`
    都过 `_hidden_session_tools`），可达性由会话内的工具表保证。
判据挪到 `test_session_only_visibility.py`（拿到了就必须看得见）。
    不排除的话这条会红在 `run_workflow` 上 —— 而那正是新语义，不是坏掉。
    """
    _boot()
    session_only = set(R._SESSION_TOOLS)
    unreachable = []
    for name in R.list_tools():
        if name in R._CORE_TOOLS or name in session_only:
            continue
        hits = [t["name"] for t in R.search_tools(name.replace("_", " "))["tools"]]
        if name not in hits:
            unreachable.append(name)
    assert not unreachable, (
        "这些工具**既非常驻、按名字也搜不到** -> 模型永远拿不到它们：\n  "
        + "\n  ".join(unreachable)
    )
    n_res = sum(1 for t in R.list_tools() if t in R._CORE_TOOLS)
    print(f"  [ok] {len(R.list_tools())} 个工具：{n_res} 个常驻，"
          f"其余按名字都能搜到")


def test_moved_tools_are_still_reachable():
    """移出常驻 ≠ 不可达：自然语言也要能搜到。

    这条会跳过未注册的工具并把它打出来 —— 本夹具不调 `init_bus`，
    因此 MCP 内置的 `web_search` / `web_fetch` 不在注册表里（生产环境里在，
    `bus/core.py` 的 `init_mcp`）。跳过是夹具的边界，不是放行：
    一个已注册的工具搜不到，这条立刻红。
    """
    _boot()
    failures, not_registered = [], []
    for name, query in _MOVED_MUST_BE_FINDABLE:
        if name not in R.list_tools():
            # 夹具覆盖不到就明说，不要静默跳过（静默跳过恰好
            # 让真正坏掉的那两个工具逃掉了）。名字列在这里却没注册 = 夹具的洞。
            not_registered.append(name)
            continue
        hits = [t["name"] for t in R.search_tools(query)["tools"]]
        if name not in hits:
            failures.append(f"{name!r} 查 {query!r} 没找到（返回 {hits[:3]}）")
    assert not not_registered, (
        f"夹具没注册这些名字（护栏覆盖不到它们）：{not_registered}"
    )
    assert not failures, (
 "被移出常驻的工具搜不到了 —— 这就是 /形状：\n "
        + "\n  ".join(failures)
    )
    print(f"  [ok] {len(_MOVED_MUST_BE_FINDABLE)} 个移出的工具都能被自然语言搜到"
          f"（夹具已含 MCP 内置）")


def test_real_phrasings_reach_their_tool():
    """同一能力的多条真实话术都要够得着。

    判据是名次，不只是"在不在"：`search_tools` 默认只回 5 条，
    排第 8 与排第 800 对模型是同一件事（都看不见）。
断言写成"必须进前 5"，与模型实际看到的那一屏同口径。
    """
    _boot()
    failures = []
    for name, query in _REACH_PHRASINGS:
        if name not in R.list_tools():
            failures.append(f"{name!r} 没注册 —— 夹具的洞，不是放行")
            continue
        top = [t["name"] for t in R.search_tools(query)["tools"]]        # limit=5
        if name in top:
            continue
        full = [t["name"] for t in R.search_tools(query, limit=200)["tools"]]
        rank = full.index(name) + 1 if name in full else None
        failures.append(f"{query!r} 里 {name!r} 没进前 5（名次 {rank}）")
    assert not failures, (
 "同一能力换个说法就够不着了 —— 这正是修完又复发的那条：\n "
        + "\n  ".join(failures)
    )
    print(f"  [ok] {len(_REACH_PHRASINGS)} 条真实话术都够得着（且都在前 5）")


def test_the_reach_phrasings_assertion_can_go_red():
    """负对照：别让我上面那条变成恒真。

    拿一条与该工具无关的话术去打 —— 它必须够不着。
    够不着才算证明上面那条有分辨力；够得着说明上面那张表在空转。
    """
    _boot()
    name, query = _REACH_NEGATIVE
    top = [t["name"] for t in R.search_tools(query)["tools"]]
    assert name not in top, (
        f"负对照失效：{query!r} 竟然命中了 {name!r}（{top}）—— "
        "上面那条断言**恒真**，什么都没测到"
    )
    print(f"  [ok] 负对照成立：{query!r} 里没有 {name!r}")


# 打分面的两个噪声源
#
# 这两条钉的是机制，不是"某条话术排第几"（那是上面 `_REACH_PHRASINGS`
# 的活）。判据：噪声不是"分不够"，是"地板随查询长度涨" ——
# 同一个 5 分，有人排第 1、有人排第 8，差别全在噪声。
# 所以下面直接问"这个源还在不在"，而不是"名次好不好"。


def test_a_common_char_scores_nothing():
    """源②：`一` / `到` / `文` 这种遍地都是的单字不许给分。

    若每个命中的字 各 +1，查询越长、描述越长，白拿的分越多。
    实测「把列宽调大一点」：`plot_excel_data` 靠 `把/列/调/一/点` 纯凑单字拿到
    5，把真答案 `format_excel_range` 的 4 反超。
判据：拿一个只有噪声、没有任何能力含义的字去搜，不该冒出一片工具。
    """
    _boot()
    for c in ("一", "到", "文", "把"):
        hits = [t["name"] for t in R.search_tools(c, limit=5)["tools"]]
        assert not hits, (
            f"搜 {c!r} 竟然返回了 {hits} —— {c!r} 出现在大部分工具的文本里，"
 "它给分就是**地板**（源②回来了）"
        )
    print("  [ok] 遍地都是的单字零分（搜它们返回空）")


def test_a_rare_char_still_reaches_its_tool():
    """负对照：兜底不许被一起砍掉（否则上面那条又变成别的病）。

    单字兜底是有理由存在的（`_query_terms` 的 docstring：jieba 会把
    「来首歌」切成 `来首`/`首歌`，而库里只写了「歌」）。`歌` 只出现在 1 个工具里
它是信号，必须给分。
    """
    _boot()
    hits = [t["name"] for t in R.search_tools("歌", limit=5)["tools"]]
    assert "search_song" in hits, (
        f"搜 '歌' 够不着 search_song（返回 {hits}）—— 单字兜底被砍过头了"
    )
    print(f"  [ok] 稀有单字仍是信号：'歌' 够得着 search_song（{hits}）")


def test_a_word_fragment_does_not_hit_a_prose_description():
    """源①：`description` 是长段中文散文，裸子串会撞上"半个词"。

    形状：`一下` ⊂「编号可以自动排序和更新」· `这个` ⊂「正是这个工具最常见的用途」
`add_word_caption` 靠 `一下` 拿 +2，把 `parse_pdf` 挤到第 5。
    判据取名次（`limit=5`，第 5 就是"刚好还看得见"，再掉一格就没了）。
    """
    _boot()
    top = [t["name"] for t in R.search_tools("读一下这个 pdf")["tools"]]
    assert top and top[0] == "parse_pdf", (
        f"「读一下这个 pdf」的第一名是 {top[0] if top else None}，不是 parse_pdf —— "
        f"散文里的二字片段又在给分了（返回 {top}）"
    )
    print(f"  [ok] 散文片段不再命中：'读一下这个 pdf' -> {top[0]}")


def test_moved_tools_are_not_in_core_anymore():
    """确认这些真的移出去了 —— 否则上面的测试是空转（它测的东西没变）。"""
    _boot()
    still = [n for n, _ in _MOVED_MUST_BE_FINDABLE if n in R._CORE_TOOLS]
    assert not still, f"这些还在常驻集里，上面的可达性测试是空的：{still}"
    print(f"  [ok] 它们确实已移出常驻集（常驻现有 {len(R._CORE_TOOLS)} 个）")


def test_index_covers_every_registered_non_resident_tool():
    """不变量：索引 ∪ 常驻 == 注册表。

    索引是模型"知道自己有什么"的唯一来源（常驻的 schema 在 `tools=` 里，
    非常驻的只有这里），一个工具既非常驻、索引里又没提，就是不可达。

    判据写成差集不许非空，不是"条目数对不对"：
        未覆盖 =（注册表 − 常驻）− 索引覆盖
    这一条与 `test_every_tool_is_either_resident_or_findable`（按名字搜得到）
    是两道闸，不要读成一条 —— 搜得到是"知道名字才行"，而模型
    不知道存在的东西搜不出来。
    """
    _boot()
    registered = set(R.list_tools())
    resident = set(R._CORE_TOOLS) & registered
    # 会话条件工具的覆盖由下面那条测试按会话保证，
    #   索引里有意不承诺它们（索引是全体会话共读的），这里必须排除，
    #   否则"索引 ∪ 常驻 == 注册表"会把两条互补的判据混成一条。
    uncovered = (registered - resident) - index_coverage() - set(R._SESSION_TOOLS)

    # 非空性守卫：非常驻集不能是空的，否则这条测试空转
    assert registered - resident, "没有非常驻工具 —— 这条测试空转了"
    assert not uncovered, (
        "这些工具**既非常驻、索引里也没提** -> 模型永远不知道它们存在：\n  "
        + "\n  ".join(sorted(uncovered)))
    print(f"  [ok] {len(registered)} 个工具：{len(resident)} 个常驻，"
          f"其余 {len(registered - resident)} 个都被索引覆盖")


def test_session_only_tools_stay_out_of_the_index():
    """会话条件工具不进索引，成对判据。

    `ask_member` 只在群会话里发。它必须同时满足两件事：
      · 不进索引 —— 索引是全体会话共读的一段文本，把"按会话算"的东西
        写进去就是一句假承诺（普通会话里模型看得到名字、调不动它）。
        代价还更实在：那是每条请求的税。
      · 但在群会话的工具表里 —— 可达性由 `get_active_tool_names` 按会话保证。

    为什么要单独立一条：上面那两条索引不变量逮不到它 ——
    没有排除的时候，它会掉进「其他已注册工具」那一桶，而那一桶本来就在覆盖里
两条不变量全绿，税照样收。判据必须钉它不在文本里。
    """
    R.register_group_tool()          # 幂等
    _boot()
    from core import groups as G

    assert R._SESSION_TOOLS, "这一条测试的前提没了（`_SESSION_TOOLS` 空了）"
    text = build_skill_index()
    #: 判据是覆盖集，不是"名字在不在文本里" —— 索引里那些兜底行是
    #:   按组渲染的（「其他已注册工具 (1 个工具): …」），一个工具名都不出现
    #:拿 `name in text` 去判，变异（把排除删掉）照样全绿
    #:   （`_leftover_groups` 真的多出那一桶、`index_coverage` 真的含它，
    #:    而文本逐字不变）。这正是"选错了观测量"那一族。
    leaked = sorted(R._SESSION_TOOLS & index_coverage())
    assert not leaked, ("会话条件工具被索引**声明覆盖**了 —— 索引是全体会话共读的"
                        "一段文本，声明它 = 对普通会话**假承诺**：\n  "
                        + "\n  ".join(leaked))
    assert not [n for n in R._SESSION_TOOLS if n in text], \
        "名字居然出现在索引正文里（说明索引的渲染方式改了，这条判据要重写）"

    # 另一半：工具表里必须有它（而且只在群会话里）
    #
    # 这一段必须进沙箱：`groups.create` 会往
    #   `data/runtime/groups.json` 落盘，而本文件没有沙箱
    #   （它历来只读索引，一个字节都不写）。漏了这一步 ——
    #   症状是"测试跑完生产 `data/runtime/` 里多一个 `{}`"，
    #   而净变化为零（建完又删了），`check_test_pollution` 的指纹法
    #   看不见它。
    from _sandbox import sandbox
    with sandbox():
        G.reset_for_tests()
        g = G.create(name="探针群", members=[{"name": "师傅", "host": True},
                                            {"name": "小飞"}])
        key = G.session_key_of(g["id"])
        in_group = R.get_active_tool_names(key)
        in_private = R.get_active_tool_names("workspace:private")
        #: `_SESSION_TOOLS` 现在有两种条件 ——
        #:   `ask_member` = 群会话；`run_workflow` = ultra 档。别用一条"都在群里"的
        #:   循环去验（那会把两个不同判据塌成一个）。
        from core import tiers
        assert "ask_member" in in_group, "群会话的工具表里没有 ask_member"
        assert "ask_member" not in in_private, "ask_member 漏进了普通会话"
        assert "run_workflow" not in in_private, "run_workflow 漏进了普通会话"
        assert "run_workflow" not in in_group, (
            "run_workflow 漏进了群会话（它只在 ultra 档发）")
        tiers.set_level("wf-probe", 6)
        assert "run_workflow" in R.get_active_tool_names("wf-probe"), (
            "ultra 档的工具表里没有 run_workflow —— 那它就真的不可达了")
        tiers.reset_tier_cache("wf-probe")
        #: 群没了就不发 `ask_member`（不是"形状像群就发"）。
        G.delete(g["id"])
        assert "ask_member" not in R.get_active_tool_names(key), \
            "群解散了还在发 ask_member —— 那是发一个明知会失败的工具"
        G.reset_for_tests()
    print("  [ok] 会话条件工具：不进索引，只在群会话的工具表里")


class _FakeReg:
    """一个够用的注册表替身 —— 用来证明兜底扫描真的会动。

    不改全局 `bus.registry` 的实态：那样这条测试的成败会取决于执行顺序
    （本文件的 `_main` 是按名字排序跑的）。
    """

    def __init__(self, names, resident=()):
        self._names = list(names)
        self._CORE_TOOLS = set(resident)

    def list_tools(self):
        return list(self._names)


def test_registry_sweep_catches_tools_no_manifest_knows_about():
    """兜底扫描本人 —— 一个不在任何清单里的工具必须自动有条目。

    这条是给上面那条不变量配的"会自己跳过"的护栏：不变量只跑真实环境，
    而真实环境今天恰好没有孤儿工具，它是绿的，但谁也没验过兜底那一半。
    这里用一个假注册表直接把它逼出来。
    """
    fake = _FakeReg(
        ["keep_me_resident", "mcp__probe_server__do_thing", "orphan_tool"],
        resident=["keep_me_resident"])
    index = build_skill_index(registry=fake)
    covered = index_coverage(registry=fake)

    assert "mcp__probe_server__do_thing" in covered, (
        "外部 MCP 服务器的工具没被兜底扫描捡起来 —— 索引会漏（`B12` 的形状）")
    assert "orphan_tool" in covered, "没归类的孤儿工具没被捡起来"
    assert "MCP 服务器 probe_server" in index, index
    assert "其他已注册工具" in index, index

    # 反方向：常驻的不许进索引 —— 它的 schema 每轮都在 `tools=` 里，
    #   索引再写一遍就是同一件事付两次钱。
    assert "keep_me_resident" not in covered, (
        "常驻工具也被写进索引了 —— 那是重复付费，不是更安全")
    print("  [ok] 兜底扫描会捡起孤儿/MCP 工具，且不重复列常驻的")


def test_leftover_groups_only_appear_when_something_is_actually_missing():
    """兜底条目只在真缺的时候才出现 —— 今天一条都没有（索引仍是 7 条 697 字符）。

    这条钉的是代价：修法不许给索引加税。真实环境的余额是 0，
    所以索引一个字节都不该变；哪天变了，一定是有工具没进清单。
    """
    _boot()
    index = build_skill_index()
    assert "其他已注册工具" not in index, (
        "出现了未归类的兜底条目 —— 说明有工具注册了却没进任何 manifest")
    assert "MCP 服务器" not in index, (
        "出现了 MCP 兜底条目 —— 外部 MCP 服务器接进来了（那是正常的，"
        "但请确认它**确实**不在常驻集里）")
    print("  [ok] 今天余额为 0：索引没有任何兜底条目（零成本）")


def test_search_tools_itself_stays_resident():
    """机制本身必须常驻 —— 没有它，整套按需发现就断了。"""
    _boot()
    assert "search_tools" in R._CORE_TOOLS
    assert "search_tools" in R.list_tools()
    print("  [ok] search_tools 仍在常驻集（按需发现的入口）")


# ------------------------------------------------------------------
# 3. 载体 —— user 侧、瞬时、不落库
# ------------------------------------------------------------------

def test_index_is_appended_to_a_copy_not_to_messages():
    """附在副本的最后一条 user 消息上，原列表一个字不动。

    原列表是要落库/继续用的那一份。注进它 = 注入物会被压缩器/滑窗
    当成对话内容。
    """
    _boot()
    msgs = [
        {"role": "system", "content": "（历史标记）"},
        {"role": "user", "content": "帮我做个表"},
    ]
    before = [dict(m) for m in msgs]

    out = append_skill_index(msgs, build_skill_index())

    assert msgs == before, "原列表被改了 —— 注入物会落库"
    assert out is not msgs, "应当返回副本"
    assert "<skill_index>" in out[-1]["content"], out[-1]["content"][:120]
    assert out[-1]["content"].startswith("帮我做个表"), "用户的话必须原样在最前面"
    # 角色序列不变（严格的 provider 要求 user/assistant 交替）
    assert [m["role"] for m in out] == [m["role"] for m in before]
    print("  [ok] 索引附在副本上（原列表不变、不为它多插一条 user 消息）")


def test_index_never_lands_in_messages_after_a_real_turn():
    """跑一整轮之后，落库的那份 `messages` 里不许有索引。

    注入物一旦进了 messages，下一轮它会被当成对话内容，
    还会被压缩器/滑窗反复搬运。
    """
    _boot()
    import core.agent_loop as AL

    class _P:
        def __init__(self):
            self.seen = []

        def chat(self, messages, tools=None, system=None, thinking=None):
            self.seen.append(messages)

            class _R:
                content = "done"
                tool_calls = None
                prompt_tokens = 1
                completion_tokens = 1
                reasoning_content = None

                @property
                def total_tokens(self):
                    return 2

            return _R()

    p = _P()
    msgs = [{"role": "user", "content": "hi"}]
    _out, out_msgs, _c, _t = AL.agent_loop(
        "hi", p, messages=msgs, max_turns=2, session_id="idx:private:probe",
    )

    # setup：索引确实发出去了（否则这条测试是空转）
    sent = "\n".join(m.get("content") or "" for m in p.seen[0])
    assert "<skill_index>" in sent, (
        "索引根本没发出去 —— 这条测试是空转，先查 _skill_index 为什么是空的"
    )
    landed = "\n".join(m.get("content") or "" for m in out_msgs)
    assert "<skill_index>" not in landed, (
        "索引落进了 messages —— 会被压缩器/滑窗当成对话内容（B3/B4 同族）"
    )
    print("  [ok] 索引发出去了，但没落进 messages")


def test_index_is_not_in_the_system_prompt():
    """载体必须是 user 侧 —— 那条禁令说的是别在 system prompt 里镜像目录。

    所以我们既不写进 system prompt，也不叫 `<available_tools>`。
    """
    _boot()
    from core.prompt_assembler import assemble_system_prompt

    sp = assemble_system_prompt("")
    assert "skill_index" not in sp, "索引跑进 system prompt 了"
    assert "<available_tools>" not in sp, "旧目录又被镜像回 prompt 了"
    assert "\n<tool_discovery>" in sp, (
        "按需发现的**机制**指引应当还在 system prompt 里（它讲的是怎么用，"
        "不是有什么）"
    )
    stats = skill_index_stats()
    print(f"  [ok] 索引不在 system prompt 里；它 {stats['entries']} 条 "
          f"{stats['chars']} 字符")


# ------------------------------------------------------------------


# ------------------------------------------------------------------
# 指令型 skill 也进索引
# ------------------------------------------------------------------

def _make_instruction_skill(root, name, desc):
    """造一个只有 `SKILL.md` 的目录（= 指令型 skill）。"""
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        "---\nname: %s\ndescription: %s\n---\n\n正文在这里。\n" % (name, desc),
        encoding="utf-8")
    return d


def test_instruction_skills_reach_the_index():
    """只有 `SKILL.md` 的目录也必须出现在索引里。

    若 `_index_groups` 只扫 `manifest.yaml`，指令型 skill 没有 manifest
模型面对它是一片空白，用户问「**不能靠"帮我做个 PPT"这种自然语言
    触发吗**」，答案是"不能"。

    拉正文不需要新增工具：索引里点出"有这么个工作方法"，
    模型自己 `read_file`（那五件全部常驻）。
    """
    import tempfile

    from core.skill_index import build_skill_index
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _make_instruction_skill(tmp, "demo-skill", "做一件很特别的事")
        tool_dir = Path(tmp) / "toolskill"
        tool_dir.mkdir()
        (tool_dir / "manifest.yaml").write_text(
            "kind: skill\nname: toolskill\ndescription: 有工具的那个\n"
            "tools: []\n", encoding="utf-8")
        text = build_skill_index(tmp)
        assert "demo-skill" in text, (
 "指令型 skill 没进索引 —— 那它**永远进不了模型面**：\n" + text)
        assert "工作方法" in text, (
            "那一行没说清它是**方法**不是工具：\n" + text)
        assert "read_file" in text, (
            "那一行没给**拉正文的办法** —— 光说有这么个东西它还是拿不到：\n" + text)
        assert "toolskill" in text, "工具型那条不该因此消失：\n" + text
    print("  [ok] 指令型 skill 进索引，且与工具型同框")


def test_no_instruction_skill_no_extra_line():
    """自造靶子：一个都没有时，不许凭空多出那一行。

    少了它，上面那条可以靠"永远加一行占位"骗过去。
    """
    import tempfile

    from core.skill_index import build_skill_index
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        text = build_skill_index(tmp)
        assert "工作方法" not in text, "没有指令型 skill 却多出了一行：\n" + text
    print("  [ok] 没有指令型 skill 时，索引里没有那一行")

def _main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
