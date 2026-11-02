"""可达性预检 —— 按真实任务的话术打一遍 search_tools，看写工具还在不在射程内。

为什么要有这一步
----------------
大量工具被移出常驻集之后 => 它们只能靠「技能索引 + `search_tools`」找到。
模型搜不到它不知道存在的东西，所以"工具还在射程内"要成立两件事：

  A. 索引要让模型知道"这类能力存在"   <- 只能摊开给人看，半机械
  B. 按真实话术去搜，真能搜回来        <- 完全机械，本脚本判死

B 判死的意义：典型的失败形状是"模型想写文件却没有写工具"
（例如"输出一份摘要到 txt"，一个触发词都不含）。A 的最终判据仍是
真实任务（人工验收）—— 本脚本只是不让那一步浪费在一个机械就能发现的问题上。

判据：查询取自真实分布 —— 用户/模型真会打的检索词，
不是构造出来的最大值。

判据是「命中」不是「只命中」：top-5 里出现必需的入口工具即通过
（多出来的名字是噪声，不是失败）。`search_tools` 默认 `limit=5`。

用法：
    cd <仓库根>
    export PYTHONIOENCODING=utf-8
    .venv/Scripts/python.exe scripts/check_skill_reach.py
"""

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: (用途, 查询, 必须命中其中之一) —— 空集 = 只看结果不断言
#: 查询一律写成模型真会打的样子（自然语言、口语），不是工具名。
CASES = [
    # 写：原失败场景
    ("写：原失败场景     ", "写一份摘要到 txt 文件",        {"create_document", "add_block"}),
    ("写：通用文件       ", "把内容写进一个文件",            {"create_document", "add_block"}),
    ("写：Word 文档      ", "创建一个 Word 文档",            {"create_document"}),
    ("写：往文档里加内容 ", "往文档里追加一段内容",          {"add_block"}),
    ("写：Excel          ", "把数据写入 Excel 表格",         {"write_excel_data", "create_document"}),
    ("写：导出           ", "把文档导出成 docx 文件",        {"export_document"}),
    ("写：PPT            ", "加一页幻灯片",                  {"add_slide", "create_svg_slide"}),
    # 读
    ("读：Word           ", "读取 Word 文档内容",            {"parse_docx"}),
    ("读：txt            ", "读一下这个文本文件",            {"parse_txt"}),
    ("读：PDF            ", "解析这个 PDF 文件",             {"parse_pdf"}),
    ("读：Excel          ", "读取 Excel 表格数据",           {"read_excel_data", "parse_xlsx"}),
    ("读：文档结构       ", "看看这个文档的大纲结构",        {"get_document_outline", "extract_document_structure"}),
    # 搜 / 摘要
    ("搜：文档内检索     ", "在这个文档里搜索关键词",        {"search_chunks"}),
    ("搜：工作区         ", "看看工作区里有哪些文件",        {"list_workspace_files"}),
    ("摘要               ", "总结一下这份文档",              {"summarize_document"}),
    # 交付 / 跨区
    ("交付文件           ", "把文件发给用户",                {"deliver_file"}),
    ("跨区搜索           ", "在别的文件夹里找找这个文件",    {"request_cross_zone_search"}),
    # `copy_to_workspace` 必须有话术覆盖。
    # 它是「跨区找到文件」到「在工作区里操作它」之间唯一的那座桥 ——
    # `merge_pdfs`/`parse_pdf` 的入口路径都过 `validate_path`，
    # 工作区外的路径它们读不了（Level 1 只放 `read`/`copy`，
    # 而 `copy` 这个操作只有 `copy_to_workspace` 在用）。
    # 而它不在常驻集里，搜不到就等于那座桥断了，
    # 症状是模型搜完跨区就卡住。
    ("跨区：复制进工作区  ", "把找到的文件复制到工作区",      {"copy_to_workspace"}),
    # 其它 skill（全部靠发现）
    ("邮件：发信         ", "发一封邮件",                    {"send_email"}),
    ("邮件：收信         ", "看看最近的邮件",                {"search_emails", "list_folders"}),
    ("联网：搜索         ", "上网搜索一下",                  {"web_search"}),
    ("联网：抓网页       ", "打开这个网页看看内容",          {"web_fetch"}),
    ("PDF：合并          ", "把几个 PDF 合并成一个",         {"merge_pdfs"}),
    ("转换：PDF -> Word  ", "把 PDF 转成 Word",              {"convert_from_pdf"}),
    ("定时任务           ", "设一个每天早上八点的定时任务",  {"cron_create"}),
    ("压缩包             ", "解开这个压缩包",                {"extract_archive"}),
    ("删除文件           ", "删掉这个文件",                  {"delete_files"}),
    ("歌曲               ", "放首歌",                        {"search_song"}),
    # PDF 页面操作族 + 图片族（11 件，全部非常驻）
    # 话术取用户/模型真会打的样子 —— 例如
    # 「把那 4 张扫描图合成一份 pdf」，那句话正是这一族存在的理由。
    ("PDF：图片合成     ", "把那 4 张扫描图合成一份 pdf",     {"images_to_pdf"}),
    ("PDF：转图片       ", "把 pdf 拆成图片",                 {"pdf_to_images"}),
    ("PDF：旋转         ", "旋转 pdf 的页面",                 {"rotate_pdf_pages"}),
    ("PDF：页序         ", "调整 pdf 的页面顺序",             {"reorder_pdf_pages"}),
    ("PDF：水印         ", "给 pdf 加水印",                   {"pdf_watermark"}),
    ("PDF：压缩         ", "压缩 pdf 文件",                   {"compress_pdf"}),
    ("PDF：加密         ", "给 pdf 设置密码",                 {"encrypt_pdf"}),
    ("图片：压缩         ", "把图片压缩一下",                 {"compress_image"}),
    ("图片：缩放         ", "图片太大缩小一点",               {"resize_image"}),
    ("图片：转格式       ", "把图片转成 jpg",                 {"convert_image_format"}),
    ("图片：旋转         ", "旋转这张图片",                   {"rotate_image"}),
    # Excel 收尾三件 + Word 表格行列四件
    # 条件格式那条尤其值得留：描述里只有 `greaterThan` 这种英文时，
    # 「分数大于60的标红」搜不到它 —— 补的是真实说法
    # （大于 / 标红），不是同义词表的发明。
    ("Excel：冻结窗格    ", "冻结表头",                       {"freeze_excel_panes"}),
    ("Excel：条件格式    ", "分数大于60的标红",               {"set_excel_conditional_format"}),
    ("Excel：数据验证    ", "加个下拉列表",                   {"add_excel_data_validation"}),
    ("Word：加行         ", "给表格加一行",                   {"add_table_rows"}),
    ("Word：加列         ", "给表格加一列",                   {"add_table_columns"}),
    ("Word：删行         ", "删掉表格里那两行",               {"delete_table_rows"}),
    ("Word：删列         ", "删除表格的某一列",               {"delete_table_columns"}),
    # Excel 写侧/分析侧的口语话术
    # 这一段补的是"判据矩阵没有靶子" —— Excel 原先只覆盖 5 条
    # （写数据 / 读数据 / 冻结表头 / 条件格式 / 下拉列表），于是
    # 46 个 Excel 工具 47/47 全绿，真实任务仍然失败（整道表格题全程
    # `run_command` + 自己写 Python，框架的 Excel 族只碰过一次 `read_excel_data`）。
    # 根因：`search_tools` 按逐词碰运气 —— 描述里字面有的词召得回，
    # 口语的说法召不回（「A 到 Y 列居中」→ `format_excel_range` 93/158）。
    #话术一律取用户会打的说法（居中 / 排名 / 去重 / 透视表 / 走势图），
    # 不是工具名，也不是描述里现成的词。
    ("Excel：居中         ", "把这张表的 A 到 Y 列居中",        {"format_excel_range"}),
    ("Excel：对齐/边框    ", "给单元格加边框和背景色",          {"format_excel_range"}),
    ("Excel：小数位       ", "把这一列的数字都改成两位小数",    {"format_excel_range"}),
    ("Excel：行高列宽     ", "调整行高列宽",                    {"format_excel_range", "auto_fit_table"}),
    ("Excel：排序         ", "按成绩从高到低排一下",            {"sort_excel_range"}),
    ("Excel：筛选         ", "筛出分数大于 60 的",              {"filter_excel_data", "set_excel_conditional_format"}),
    ("Excel：去重         ", "把重复的行去掉",                  {"remove_excel_duplicates"}),
    ("Excel：透视表       ", "做个数据透视表",                  {"create_excel_pivot"}),
    ("Excel：分类汇总     ", "按分类汇总一下",                  {"create_excel_pivot"}),
    ("Excel：图表         ", "生成一个柱状图",                  {"create_excel_chart", "plot_excel_data"}),
    ("Excel：走势图       ", "画个走势图",                      {"plot_excel_data", "create_excel_chart"}),
    ("Excel：重算公式     ", "重算一下表格里的公式",            {"recalculate_excel_formulas"}),
    ("Excel：合并单元格   ", "把这两个单元格合并居中",          {"merge_excel_cells"}),
    ("Excel：合并工作簿   ", "合并多个 Excel 文件",             {"merge_excel_workbooks"}),
    ("Excel：保护工作表   ", "把工作表保护起来",                {"protect_excel_sheet"}),
    ("Excel：表结构       ", "这张表有几行几列、每列都是什么",  {"profile_excel_data"}),
    ("Excel：定位值       ", "找出这个值在第几行",              {"find_excel_value"}),
]


def _print_index(index_text: str) -> None:
    print("=" * 68)
    print("会发给模型的技能索引（<skill_index> 里的正文，原样）")
    print("=" * 68)
    if not index_text:
        print("  [!] 索引为空 —— 模型看不到任何能力目录，全靠瞎猜")
        return
    for line in index_text.splitlines():
        print("  " + line)
    print(f"\n  合计 {len(index_text)} 字符 / {len(index_text.splitlines())} 条")
    print()


def _run_cases() -> tuple[int, int, set]:
    """打一遍矩阵。返回 (失败数, 总数, 出现过的工具名集合)。

    判据只对"非常驻"的工具成立：
    常驻工具本来就在 `tools=` 里，模型直接看得见，它搜不搜得到
    `search_tools` 完全不影响可达性。把常驻工具判成 FAIL
    不是发现问题，是判据写错了。真正该判的是：
    移出常驻 = 只能靠检索 = 搜不到就是黑的。
    """
    from bus.registry import _CORE_TOOLS, search_tools

    core = set(_CORE_TOOLS)
    seen: set = set()
    failed = 0
    print("=" * 68)
    print("真实话术 -> search_tools 命中矩阵（默认 limit=5）")
    print("=" * 68)
    for label, query, required in CASES:
        res = search_tools(query)
        names = [t["name"] for t in res.get("tools", [])]
        seen.update(names)

        # 只有"需要靠检索才能拿到"的那些才算数
        need = required - core
        found = need & set(names)
        if not required:
            mark = "  ? "
        elif not need:
            mark = " ok "          # 要求的全是常驻，本就不靠检索
        elif found:
            mark = "PASS"
        else:
            mark = "FAIL"
            failed += 1

        print(f"  [{mark}] {label} {query}")
        print(f"          -> {', '.join(names) if names else '(无命中)'}")
        if mark == "FAIL":
            print(f"          [!!] 需检索但没搜到: {', '.join(sorted(need))}"
                  f"  (常驻的: {', '.join(sorted(required & core)) or '无'})")
    print()
    return failed, len(CASES), seen


def _report_shadowed_core(seen: set) -> None:
    """常驻、但按真实话术搜不回来的工具。

    今天无害（它们已经在 `tools=` 里，模型直接看得见），但这份名单是
    「一旦移出常驻就会立刻变黑」的候选 —— 要动常驻集时先看它。
    """
    from bus.registry import _CORE_TOOLS, get_tool_schemas

    registered = {td["function"]["name"] for td in get_tool_schemas()}
    core = set(_CORE_TOOLS) & registered
    dark = sorted(core - seen)
    print("=" * 68)
    print("常驻但搜不回来的（今天无害，移出常驻就会变黑）")
    print("=" * 68)
    print(f"  常驻 {len(core)} 个，其中 {len(dark)} 个在上述查询里一次没出现")
    if dark:
        for i in range(0, len(dark), 4):
            print("    " + ", ".join(dark[i:i + 4]))
    print()


def _report_unreachable(seen: set) -> None:
    """从未在任何真实话术下出现过的工具 —— 长尾有多少、是哪些。"""
    from bus.registry import _CORE_TOOLS, get_tool_schemas

    all_names = {td["function"]["name"] for td in get_tool_schemas()}
    non_core = all_names - set(_CORE_TOOLS)
    never = sorted(non_core - seen)
    print("=" * 68)
    print("长尾：从未在任何查询里出现过的**非常驻**工具")
    print("=" * 68)
    print(f"  非常驻共 {len(non_core)} 个，其中 {len(never)} 个一次都没露面")
    if never:
        print("  （不是失败 —— 这些是「模型得自己想到关键词」的深尾；")
        print("    但如果里面有你**预期很常用**的，那就是索引措辞的靶子）")
        for i in range(0, len(never), 4):
            print("    " + ", ".join(never[i:i + 4]))
    print()


def main() -> int:
    # 控制台可能是 GBK；索引正文含中文与可能的非 GBK 字符。
    # 不静默替换成乱码、也不让整个脚本崩掉。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    os.chdir(_PROJECT_ROOT)

    # 复用 measure_request 的最小装载（不调 init_bus，不联网）。
    # 刻意 import 而不是抄一份：装载逻辑有两份时，夹具会漏注册 MCP
    # => 护栏恰好跳过真正坏掉的那两个工具。
    from measure_request import boot_registry

    from core.skill_index import build_skill_index

    skills, tools = boot_registry()
    from bus.registry import _CORE_TOOLS

    print(f"[preflight] 注册表就绪：{skills} 个 skill、{tools} 个工具"
          f"（常驻 {len(_CORE_TOOLS)} 个）\n")

    _print_index(build_skill_index())

    failed, total, seen = _run_cases()
    print("-" * 68)
    print(f"  {total - failed}/{total} 通过"
          f"（判据只对**非常驻**工具成立；常驻的标 [ ok ]，本就不靠检索）")
    print("-" * 68)
    print()

    _report_shadowed_core(seen)
    _report_unreachable(seen)

    print("=" * 68)
    print("这只是机械的那一半。**真实任务**仍然必须跑：")
    print("  读一个 workspace 文档 -> 输出摘要到 txt，看它要不要绕 search_tools、")
    print("  绕回来的是不是 create_document / add_block。")
    print("=" * 68)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
