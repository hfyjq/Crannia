"""压力测试的夹具生成器 —— 造一份超长文档，给端到端压力测试用。

为什么要有它
============
压力测试要回答的是「小米 300 页年报那类东西，跑起来会怎样」。
没有夹具就得临时找一份大文档，于是每次都跑不成，
而"跑不成"与"跑起来没问题"在清单里长得一样。

把它变成一条命令：夹具可复现（固定种子）、自描述（带哨兵）、
尺寸可调（`--pages`）。

它测什么（→ 读数表）
--------------------
1. 解析层：`office_parser` 读 300 页 docx 会不会爆内存 / 会不会真的流式
2. 摘要层：`summarizer` 是单次调用还是 map-reduce 扇出（硬约束 4：
   装得下必须走单次）
3. 截断层：工具结果 6,000 字符上限到了会不会出声（判据 4：兜底不可用 = 没有兜底）
4. 读到底没有：三个哨兵分别在开头 / 中间 / 结尾 —— 模型报出了几个，
   就是它实际读到了哪里

哨兵那一条是这份夹具存在的核心理由：曾有一次探针造小了
（200KB 没触到门）—— 而没触到门与通过了，读数一模一样。
哨兵把"读到哪"变成可判定的：报不出 `PROBE-C`，尾部根本没进上下文。

用法::

    cd <仓库根>
    .venv/Scripts/python.exe scripts/make_stress_fixture.py              # 默认 ~300 页
    .venv/Scripts/python.exe scripts/make_stress_fixture.py --pages 60   # 先跑小的摸底
    .venv/Scripts/python.exe scripts/make_stress_fixture.py --out "workspace/年报.docx"

产出落在 `data/runtime/` 下（`.gitignore` 里），不写工作区、不写生产目录
（测试不许碰生产数据 —— 这条夹具同理，它是"造给测试用的"）。
要放到工作区让 agent 读，用 `--out` 显式指定。
"""

import argparse
import io
import os
import random
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ASCII-only 输出 —— Windows 控制台默认 GBK，emoji 会让整个运行崩掉
# （而崩掉的输出看起来像测试失败）。

#: 每页大致多少中文字符（A4 / 默认页边距 / 小四）。是个估计，
#: 所以本脚本报的是字符数（那是真正的成本量纲），页数只是给人看的。
CHARS_PER_PAGE = 600

#: 哨兵 —— 故意用 ASCII + 数字，方便在模型的回答里无歧义地认出它。
PROBE_HEAD = "[PROBE-A-4F2C]"
PROBE_MID = "[PROBE-B-91E7]"
PROBE_TAIL = "[PROBE-C-7D33]"

#: 一根"针"：藏在文档中段的一个具体数字。模型要答对它，必须真的读到那儿。
NEEDLE = "739184"

_CH = [
    "报告期内，公司围绕主营业务持续加大研发投入，产品结构进一步优化。",
    "受宏观环境影响，上下游价格波动加大，公司通过集中采购与长期协议平抑成本。",
    "本年度新签订单同比增长，其中海外业务占比提升，结算周期相应拉长。",
    "为提升供应链韧性，公司在多地布局仓储，缩短了平均交付时长。",
    "研发方面，报告期内新增授权专利若干项，覆盖核心工艺环节。",
    "销售费用率同比下降，主要系渠道结构调整与线上占比提升所致。",
    "公司持续完善内控体系，聘请外部机构对关键流程进行专项审计。",
    "在人才方面，公司推行了新的激励方案，核心团队保持稳定。",
    "产能利用率维持在较高水平，技改项目按计划推进。",
    "现金流方面，经营性现金流净额与净利润的匹配度较上年改善。",
    "公司主要客户集中度有所下降，客户结构更加均衡。",
    "面对行业竞争，公司通过差异化定价与增值服务维持毛利水平。",
    "报告期内未发生重大安全、环保事故，合规体系运行有效。",
    "为应对汇率波动，公司适度使用了远期结售汇等工具进行对冲。",
    "存货周转天数较上年同期缩短，运营效率提升。",
    "公司完成了对下属子公司的股权整合，管理层级进一步压缩。",
    "在数字化转型方面，公司上线了新的生产管理系统，数据打通至车间。",
    "客户满意度调查结果显示，交付及时率与售后响应速度均有改善。",
    "公司已就未来三年的资本开支作出规划，资金来源以自有资金为主。",
    "本报告经董事会审议通过，并已提请股东大会审议。",
]

_HEADINGS = [
    "经营情况讨论与分析", "主要财务指标", "业务回顾", "研发与创新",
    "生产与供应链", "销售与市场", "人力资源", "风险因素",
    "公司治理", "内部控制", "环境与社会责任", "未来展望",
]

_SECTIONS = ["总体情况", "分项说明", "同比分析", "重点事项", "下一年度计划"]


def _para(rng, lo=3, hi=6):
    """一段正文：`lo`~`hi` 句。"""
    return "".join(rng.choice(_CH) for _ in range(rng.randint(lo, hi)))


def build_docx(target_chars, seed=20261001):
    """造文档，返回 `(docx.Document, 实际字符数)`。"""
    from docx import Document

    rng = random.Random(seed)
    doc = Document()

    chars = 0

    def add(text, style=None):
        nonlocal chars
        doc.add_paragraph(text, style=style)
        chars += len(text)

    add("示例集团股份有限公司 年度报告（压力测试夹具）", style="Title")
    add(PROBE_HEAD)
    add("本文件由 scripts/make_stress_fixture.py 生成，用于端到端压力测试，内容为虚构。")

    chapter = 0
    mid_done = False
    tail_done = False
    while chars < target_chars:
        chapter += 1
        add("第 %d 篇  %s" % (chapter, _HEADINGS[chapter % len(_HEADINGS)]), style="Heading 1")

        for si, sec in enumerate(_SECTIONS, start=1):
            add("第 %d 节  %s" % (si, sec), style="Heading 2")
            for _ in range(rng.randint(2, 4)):
                add(_para(rng))

            # 每两节插一张表 —— 让解析器也过一遍表格路径（pdfplumber/openpyxl 同族）
            if si % 2 == 0:
                t = doc.add_table(rows=4, cols=3)
                t.style = "Table Grid"
                for r in range(4):
                    for c in range(3):
                        cell = t.cell(r, c)
                        cell.text = "科目%d-%d" % (r, c) if r == 0 else str(rng.randint(100, 9999))
                chars += 4 * 3 * 8

        # 中段针 + 中段哨兵：藏在过半的地方，前 50% 的读数答不出它
        if not mid_done and chars >= target_chars * 0.5:
            add(PROBE_MID)
            add("★ 关键经营指标：本年度营业收入为 %s 万元。该数字后文不再重复。" % NEEDLE)
            mid_done = True

    # 结尾哨兵：必须在最后一段。模型报不出它，尾部根本没进上下文。
    add(PROBE_TAIL)
    add("全文结束。以上内容为虚构，仅用于压力测试。")
    tail_done = True
    if not mid_done:  # 目标太小时也不能少 —— 中段哨兵补在尾部之前
        add(PROBE_MID)

    return doc, chars, tail_done


def verify(path, target_chars):
    """读过一遍再宣布成功 —— 生成物要自检（同 `--check` 那一族）。

    验三件事，每一条都是"夹具是不是它自称的那个东西"：
    ① 三个哨兵都在（少一个，"读到哪"就判不出来）；
    ② 针只出现一次（重复了就成了"多处出现"，模型答对也不说明读到中段）；
    ③ 中段哨兵真的在过半之后（落在开头就白设了）。

    返回 `(ok, notes)`。
    """
    import docx

    d = docx.Document(str(path))
    paras = [p.text for p in d.paragraphs]
    notes = []

    for name, probe in (("head", PROBE_HEAD), ("mid", PROBE_MID), ("tail", PROBE_TAIL)):
        if probe not in "\n".join(paras):
            notes.append("MISSING probe %s (%s)" % (name, probe))

    n_needle = "\n".join(paras).count(NEEDLE)
    if n_needle != 1:
        notes.append("needle %s appears %d times (want exactly 1)" % (NEEDLE, n_needle))

    idx_mid = next((i for i, t in enumerate(paras) if PROBE_MID in t), None)
    if idx_mid is not None and paras and idx_mid < len(paras) * 0.5:
        notes.append("mid probe at paragraph %d/%d -- NOT past the halfway mark"
                     % (idx_mid, len(paras)))

    return (not notes), notes


def main():
    ap = argparse.ArgumentParser(description="造 T1 的超长文档夹具")
    ap.add_argument("--pages", type=int, default=300,
                    help="目标页数（按每页 %d 字符估）" % CHARS_PER_PAGE)
    ap.add_argument("--out", default=None,
                    help="输出路径（默认 data/runtime/stress/年报压力测试.docx）")
    ap.add_argument("--seed", type=int, default=20261001, help="随机种子（默认固定）")
    args = ap.parse_args()

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = _PROJECT_ROOT / out
    else:
        out = _PROJECT_ROOT / "data" / "runtime" / "stress" / "\u5e74\u62a5\u538b\u529b\u6d4b\u8bd5.docx"

    target_chars = max(1, args.pages) * CHARS_PER_PAGE
    doc, chars, _ = build_docx(target_chars, seed=args.seed)

    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out))

    size = out.stat().st_size
    ok, notes = verify(out, target_chars)

    print("=" * 62)
    print("T1 stress fixture written")
    print("=" * 62)
    print("path      : %s" % out)
    print("chars     : %d  (target %d)" % (chars, target_chars))
    print("est pages : %d  (%d chars/page)" % (round(chars / CHARS_PER_PAGE), CHARS_PER_PAGE))
    print("bytes     : %d  (%.1f KB)" % (size, size / 1024.0))
    print("seed      : %d" % args.seed)
    print()
    print("probes    : %s (head) / %s (mid) / %s (tail)" % (PROBE_HEAD, PROBE_MID, PROBE_TAIL))
    print("needle    : %s  -- in the middle, never repeated" % NEEDLE)
    print()
    if ok:
        print("self-check: PASS -- 3 probes present, needle unique, mid probe past halfway")
    else:
        print("self-check: FAIL")
        for n in notes:
            print("    - %s" % n)
    print()
    print("Next: fill in 压力测试-读数表.md after one real run.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
