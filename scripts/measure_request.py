"""量清「一条请求到底装了什么」—— 每一个数都出自同一个仪器。

## 为什么需要它

固定开销是纯税：每条请求都交，与任务大小无关；而历史是劳动成果，砍了就丢活。
所以先把固定开销量出来，再决定砍哪里。

一次读到：回合开始时的上下文中位 8,384、最大 18,379 tokens，
而压缩阈值在 111,411（差 6~13 倍）—— 这说明固定开销才是该动的地方。

本脚本不调用 `init_bus()` —— 那会加载 MCP 与 IM 通道（真连网）。这里只装载
注册表需要的两样：框架级 `search_tools` + 各 skill 的清单（`on_demand` 走
`load_skill_lazy`，只读 manifest、只注册桩，不 import handler.py，因此没有副作用）。

每个数都整条量，不求和：所有数都走 `provider.estimate_request_tokens(...)`，
不做 `sum(count_tokens(单条))` —— 后者是抄一份公式，JSON 框架开销会让它和整体对不上。

用法：
    .venv/Scripts/python.exe scripts/measure_request.py
"""

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import yaml  # noqa: E402

# ASCII-only 输出（Windows 控制台默认 GBK；emoji 会让整个运行崩掉）。
SESS = "measure:private:probe"


def _fmt(n) -> str:
    return f"{n:,}"


def boot_registry() -> tuple[int, int]:
    """最小装载：search_tools + 各 skill 的清单。返回 (skill 数, 工具数)。"""
    import bus.registry as R

    R.register_search_tool()
    # run_command 也是框架级工具（生产里由 `init_bus` 注册）。
    # 漏了它，这份测量就会少算一个常驻工具。
    R.register_command_tool()
    R.register_subagent_tool()
    # 工作流工具也是框架级（生产里由 `init_bus` 注册）——
    # 这里漏了，`_ISOLATED_DOC_TOOLS` 里那个名字就会"注册表里不存在"。
    R.register_workflow_tool()
    # todo_write 同理：漏了它，下面那道自检会把"漏了谁"变成一条响亮的
    # 报错，而不是一个偏小的数。
    R.register_todo_tool()
    R.register_task_tool()
    #: 会话条件工具（只在压力窗口发给模型）—— 它不进常驻集；
    #:   注册它只是让这份最小装载与 `init_bus` 同口径。
    R.register_compaction_tool()
    # 权限表里有的名字如果没被注册，注册表幂等测试会当场红。
    R.register_ice_reveal_tool()
    # 文件原语五件套、skill 工厂同理：都是框架级工具，漏一个就少算一个。
    R.register_file_tools()
    R.register_skill_factory_tool()
    R.register_memory_tools()
    R.register_history_tool()
    # 计划模式两件同理。加完常驻集后先跑这里，自检会直接点名没装进来的名字。
    R.register_plan_tools()
    # 周期任务三件同理：同样由常驻集自检兜住。
    R.register_cron_tools()
    # ask_member 不在常驻集里，所以上面那道"常驻集自检"逮不到它；
    # 逮到它的是"权限表里的名字必须真注册过"那条测试。
    R.register_group_tool()
    # ask_user 在常驻集里，上面的自检会直接点名它。
    R.register_ask_user_tool()
    # computer 只在持有接管的会话里发出，不在常驻集里，自检逮不到它；
    # 兜住它的是"隔离名单里的名字必须真注册过"那条测试。
    R.register_computer_tool()
    R.register_browser_tool()
    # 符号查询：生产里由 `init_bus` 注册。没配语言服务器时它不进 `tools=`
    # （`registry._gated_tools`），所以这一步不会让下面的读数变大；
    # 但不注册的话，"隔离/权限表里的名字必须真注册过"那几条会红。
    R.register_code_intel_tool()
    # 工具编排：生产里同样由 `init_bus` 注册；漏了它，显式表里那些名字
    # 会变成"注册表里不存在"。
    R.register_program_tool()

    # MCP 内置工具（web_search / web_fetch）也必须注册 —— 生产里由
    # `init_mcp` 注册。漏掉它们会让这份测量少算两个工具，并把它们从
    # 常驻集里悄悄滤掉。
    # `register_tools()` 只注册 schema，不联网（已核实）。
    try:
        from mcpx.servers import web_search as _mcp_web
        _mcp_web.register_tools()
    except Exception as exc:
        print(f"  [!] MCP 内置工具未注册（{type(exc).__name__}: {exc}）"
              f" —— 下面的数会偏小")

    from bus.loaders.skill_loader import load_skill, load_skill_lazy

    # 装载各 skill 的清单（只读 manifest、只注册桩，不 import handler）
    skills = 0
    for entry in sorted(Path("skills").iterdir()):
        manifest_path = entry / "manifest.yaml"
        if not (entry.is_dir() and manifest_path.exists()):
            continue
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            print(f"  [skip] {entry.name}: {type(exc).__name__}: {exc}")
            continue
        if manifest.get("draft"):
            continue
        strategy = manifest.get("load_strategy", "eager")
        try:
            # guard 可省：懒加载路径只读 manifest、只注册桩，从不 import handler.py
            if strategy == "on_demand":
                load_skill_lazy(entry)
            else:
                load_skill(entry)
            skills += 1
        except Exception as exc:
            print(f"  [skip] {entry.name}: {type(exc).__name__}: {exc}")

    # 出站：生产里由 `init_bus` 在技能装载之后注册（它的描述里列已注册的
    # 动作名）。漏了它，显式权限表里的 `send_outbound` 会变成"注册表里不存在"，
    # 而那一格档位就静默失效。放在这里与生产同序。
    R.register_outbound_tool()

    # 自检：常驻集里的每一个都必须真被装进来
    #
    # 必须放在技能装载之后：`_CORE_TOOLS` 里一多半是技能工具
    # （`add_block` / `create_document` / …）—— 放在技能循环之前会对着
    # 尚未装进来的技能工具误报。
    #
    # 漏掉一个注册调用时，本脚本会少算一个常驻工具，而少算的数
    # 看起来完全像一个结论。把"漏了谁"从"数偏小"变成"当场报错"。
    # 不在 `init_bus` 里查：生产那条路上 MCP 是稍后由 `init_mcp`
    # 注册的，在那儿查会误报。
    _missing = sorted(set(R._CORE_TOOLS) - set(R.list_tools()))
    if _missing:
        raise RuntimeError(
            "常驻集里有 %d 个工具没被装进来：%s —— 本函数少调了注册"
            "（注册表与装载必须同一份：MCP、命令、常驻集各踩过一次），"
            "下面每一个数都会偏小"
            % (len(_missing), _missing))

    return skills, len(R.list_tools())


def boot_provider(config: dict):
    """实例化当前配置的那个 provider（不联网；构造期不发请求）。"""
    import bus.registry as R
    from bus.loaders.provider_loader import load_provider

    name = config.get("model", {}).get("provider", "deepseek")
    load_provider(Path("providers") / name, config)
    return R.get_provider(name)


def measure(config: dict) -> dict:
    from bus.registry import (
        _CORE_TOOLS, get_tool_schemas, get_tool_schemas_active, get_discovered,
    )

    provider = boot_provider(config)

    def part(messages=(), system=None, tools=None) -> dict:
        """唯一入口 —— 所有数都从这里出。"""
        return provider.estimate_request_tokens(
            list(messages), system=system, tools=tools
        )

    # system prompt（每轮重建的那份，不含 memory_context）
    # 必须带上 model_name —— 否则这个仪器量到的比生产少身份行里那截
    # 模型名，仪器与生产不同源。
    from core.prompt_assembler import assemble_system_prompt, model_name_of

    system_text = assemble_system_prompt("", model_name=model_name_of(provider))
    sys_bd = part(system=system_text)

    # 工具表：本轮 wire 集 vs 全量注册表
    active = get_tool_schemas_active(SESS)
    all_tools = get_tool_schemas()
    active_bd = part(tools=active)
    all_bd = part(tools=all_tools)

    # 每个常驻工具的边际成本（留一法，不求和）
    name_of = {id(t): (t.get("function", {}).get("name") or t.get("name", "?"))
               for t in active}
    full = active_bd["tools"]
    per_tool = []
    for i, t in enumerate(active):
        reduced = active[:i] + active[i + 1:]
        delta = full - part(tools=reduced)["tools"]
        per_tool.append((name_of[id(t)], delta))
    per_tool.sort(key=lambda kv: -kv[1])

    # 记忆注入（只读：get_context 不写库）
    mem_chars, mem_tokens = 0, 0
    # 对账用的一对测量（缺省 `ok=False`，报告里会如实说"没对上账"，
    # 而不是拿两个 0 冒充一致 —— 读不到 ≠ 0）。
    recon = {"ok": False, "raw": 0, "total": 0}
    try:
        from core.memory.manager import MemoryManager

        mgr = MemoryManager(config, provider=provider)
        ctx_text = mgr.get_context(SESS, "测量探针") or ""
        mem_chars = len(ctx_text)
        if ctx_text:
            mem_tokens = part(system=ctx_text)["system"]
        thresholds = mgr.describe_thresholds()
        # 对账
        # 两侧必须同量纲：用一次 `part()` 同时带上 system 与 tools，
        # 拿它的 `raw` 对 `nh["raw"]`、`total` 对 `nh["value"]`。
        # 不能拿"乘过因子"的数去比 RAW —— 因子接近 1 时看起来像对上了，
        # 实际上从未对过账。
        # 两侧还必须同 session_id：`describe_thresholds` 走的是
        # `_non_history_cost("")`（启动自检那一刻还没有会话），
        # 所以这里也取 `""` 才可比。
        # 会话有已发现工具时两者本来就不该相等 —— 那不是 bug，
        # 是"量的是两个会话"。
        _recon = part(system=assemble_system_prompt(
            "", model_name=model_name_of(provider)),
            tools=get_tool_schemas_active(""))
        recon = {"ok": True, "raw": int(_recon["raw"]), "total": int(_recon["total"])}
        mgr.close()
    except Exception as exc:
        print(f"  [warn] 记忆注入测量失败：{type(exc).__name__}: {exc}")
        thresholds = {}

    # 技能粒度索引的成本（每轮第一条请求都要付的那份）
    index_lines = []
    for entry in sorted(Path("skills").iterdir()):
        mf = entry / "manifest.yaml"
        if not (entry.is_dir() and mf.exists()):
            continue
        m = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
        if m.get("draft"):
            continue
        n_tools = len(m.get("tools", []) or [])
        desc = (m.get("description") or "").strip().replace("\n", " ")[:80]
        index_lines.append(f"- {entry.name} ({n_tools} 工具): {desc}")
    index_text = "\n".join(index_lines)
    index_tokens = part(messages=[{"role": "user", "content": index_text}])["messages"]

    return {
        "provider": type(provider).__name__,
        "model": config.get("model", {}).get("model_name", "?"),
        "window": provider.context_window,
        "system_tokens": sys_bd["system"],
        "system_chars": len(system_text),
        # 校准因子 —— 只为了让报表能同时给出 RAW 与乘完因子那个数。
        "factor": sys_bd["factor"],
        "active_count": len(active),
        "active_tokens": active_bd["tools"],
        "all_count": len(all_tools),
        "all_tokens": all_bd["tools"],
        "per_tool": per_tool,
        "core": sorted(_CORE_TOOLS),
        "discovered": sorted(get_discovered(SESS)),
        "mem_chars": mem_chars,
        "mem_tokens": mem_tokens,
        "index_lines": index_lines,
        "index_tokens": index_tokens,
        "thresholds": thresholds,
        "recon": recon,
    }


def report(m: dict) -> int:
    W = 62
    print("=" * W)
    print("一条请求的体积构成（同一个仪器：estimate_request_tokens）")
    print("=" * W)
    print(f"  provider : {m['provider']}  model={m['model']}")
    print(f"  窗口     : {_fmt(m['window'])} tokens")
    print()

    fixed = m["system_tokens"] + m["active_tokens"] + m["mem_tokens"]
    factor = float(m.get("factor") or 1.0)
    factored = int(round(fixed * factor))
    print("  ── 固定开销（每条请求都交，与任务大小无关）──")
    # 本表打的是 `parts`（RAW，未乘因子） —— 别拿它跟另一个面板对，
    #    面板打的是已乘因子的（`_factored`，且它的「合计」用的是 `total`）。
    #    面板 `工具表 5,281` vs 本表 `5,739`，而 `5,739 x 0.9202 = 5,281`
    #    —— 两个数都对，是两个量纲。
    #两边都没错，错的是没写明。这一行就是为了不再让人花半小时排查。
    print(f"    （下表是 **RAW**，未乘因子；乘完是 {_fmt(factored)}）")
    print(f"    system prompt   {_fmt(m['system_tokens']):>10}  ({m['system_chars']:,} 字符)")
    print(f"    工具表({m['active_count']:>3} 个) {_fmt(m['active_tokens']):>10}")
    print(f"    记忆注入        {_fmt(m['mem_tokens']):>10}  ({m['mem_chars']:,} 字符)")
    print(f"    ------------------------------")
    print(f"    合计            {_fmt(fixed):>10}   (RAW；x {factor:.4f} = {_fmt(factored)})")
    print()
    print(f"  ── 全量注册表（{m['all_count']} 个工具）──")
    print(f"    {_fmt(m['all_tokens'])} tokens"
          f"  = 常驻的 {m['all_tokens'] / max(m['active_tokens'], 1):.1f} 倍")
    print()
    print("  ── 技能索引（它已经在用，不是反事实）──")
    for line in m["index_lines"]:
        print(f"    {line}")
    print(f"    索引合计        {_fmt(m['index_tokens']):>10}"
          f"  ({len(m['index_lines'])} 条)")
    print()

    # 前后对照
    # 「之前」是用本脚本同一个仪器量下来的值（22 个常驻工具）。
    # 写死在这里是故意的：它是基线，不是从当前配置推出来的 ——
    # 当前配置改了，基线不该跟着动。
    _BEFORE = {"active_count": 22, "active_tokens": 4486,
               "note": "2026-09-25 之前；22 个常驻工具（含 MCP 的 2 个）"}
    delta_tools = m["active_tokens"] - _BEFORE["active_tokens"]
    net = m["active_tokens"] + m["index_tokens"] - _BEFORE["active_tokens"]
    print("  ── 前后对照（同一仪器）──")
    print(f"    常驻工具   {_BEFORE['active_count']:>3} 个 {_fmt(_BEFORE['active_tokens']):>8}"
          f"  ->  {m['active_count']:>3} 个 {_fmt(m['active_tokens']):>8}")
    print(f"    技能索引   {0:>3} 个 {_fmt(0):>8}"
          f"  ->  {len(m['index_lines']):>3} 条 {_fmt(m['index_tokens']):>8}")
    print(f"    ------------------------------------")
    print(f"    工具表净变化      {delta_tools:+,}")
    print(f"    **含索引的净变化  {net:+,}**（每轮第一次请求付它，之后不再付）")
    print(f"    [!] 基线说明：{_BEFORE['note']}")
    print()

    print("  ── 常驻工具逐个的边际成本（留一法）──")
    for name, delta in m["per_tool"][:12]:
        print(f"    {name:<34} {_fmt(delta):>8}")
    if len(m["per_tool"]) > 12:
        rest = sum(d for _, d in m["per_tool"][12:])
        print(f"    {'(其余 %d 个)' % (len(m['per_tool']) - 12):<34} {_fmt(rest):>8}")
    print()

    th = m["thresholds"] or {}
    rc = 0
    if th:
        ct = th.get("compression_threshold", {})
        nh = th.get("non_history_measured", {})
        rec = m.get("recon") or {"ok": False}
        # 两侧同量纲 —— 见 `measure()` 里那段注释：
        #    不能拿"乘过因子的"比"RAW"。
        d_raw = rec["raw"] - int(nh.get("raw", 0)) if rec.get("ok") else None
        d_tot = rec["total"] - int(nh.get("value", 0)) if rec.get("ok") else None
        print("  ── 对账（与 describe_thresholds **同量纲**比）──")
        print(f"    {'':<28}{'RAW':>10}  {'x factor':>10}")
        print(f"    describe_thresholds        {_fmt(nh.get('raw', 0)):>10}  "
              f"{_fmt(nh.get('value', 0)):>10}")
        print(f"    本脚本（同取 session_id='') {_fmt(rec.get('raw', 0)):>10}  "
              f"{_fmt(rec.get('total', 0)):>10}")
        if d_raw is None:
            print("    [!] 对账**没跑成**（记忆注入那一段抛了）—— 不是\"一致\"，是**没测**")
            rc = 1
        else:
            print(f"    差                         {d_raw:>+10}  {d_tot:>+10}")
            if d_raw or d_tot:
                print("    [!] 对不上 —— 两个仪器**同源同量纲**，不该有差。"
                      "查 `estimate_request_tokens` 是不是被绕开了一处。")
                rc = 1
            else:
                print("    -> 两个仪器一致（同源 · 同量纲 · 同 session_id）")
        print("    [!] `describe_thresholds` 取的是 **空 session_id**"
              "（启动自检那一刻还没有会话）-> 上面那两行**刻意**都取空串。"
              f"本表上面的固定开销取的是 `{SESS}`，会话有已发现工具时"
              "**本来就不该相等** —— 那不是 bug，是量的是两个会话。")
        print(f"    压缩阈值                       : {_fmt(ct.get('value', 0))}"
              f"  (window x {ct.get('ratio', '?')})")
        gate = ct.get("value", 0)
        if gate:
            print(f"    固定开销 / 阈值                : {fixed / gate * 100:.1f}%")
        print(f"    [!] 记忆注入**不在** describe_thresholds 里（它只算 system+tools）")
    print("=" * W)
    return rc


def main() -> int:
    # 与 main.py / web/server.py 同一套加载方式（否则 provider 构造期会因
    # 空 key 直接抛 OpenAIError）。不打印任何 key。
    from dotenv import load_dotenv

    load_dotenv(_PROJECT_ROOT / ".env", override=True)

    from bus.core import load_config

    config = load_config(Path("config.yaml"))
    skills, tools = boot_registry()
    print(f"[measure] 注册表就绪：{skills} 个 skill、{tools} 个工具")
    return report(measure(config))


if __name__ == "__main__":
    sys.exit(main())
