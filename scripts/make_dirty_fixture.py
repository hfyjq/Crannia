"""生成「脏表」夹具 + 答案卡 —— 用于验证读表能力在换一张表时仍然成立。

## 为什么要有它

`read_excel_data` 已经为一张真实表（`AAAA公司）2026年09月度经营总结.xlsx`）
做过三个方向的加固。判据是：这张表能读，下次换张表行不行？
-> 需要一个故意跟那张表长得不一样的夹具，在换表之前先在自己家里撞一次。

## 它故意脏在哪（每一条都对应一个已知失败形态）

| # | 脏点 | 对应 |
|---|---|---|
| 1 | 表头不在第 1 行（上面有标题 + 说明 + 空行） | VLDB `header omission` |
| 2 | 行名不在 A 列（A 列空 / 是序号） | 丢标签列 |
| 3 | 一个 sheet 里上下两张表（中间空行隔开） | multi-table detection |
| 4 | 纵向合并的行分组标签（只在左上角） | `merge_mode` / merged-cell collapse |
| 5 | 块尾有一行「合计」（不是新科目） | 模型把它当数据行再算一遍 |
| 6 | 「本年」与「上年同期」相邻两行（数值接近） | 把两列混读 |
| 7 | 张数 ≠ 9（只有 3 张） | 模型对"9 张月表"的先验 |
| 8 | 列名带空格 / 全角括注 | 按名字定位会失手 |
| 9 | 一张表里两种口径并存（当月 vs 累计） | 口径混读 |

## 用法

    .venv/Scripts/python.exe scripts/make_dirty_fixture.py

产出（都在 `workspace/`，两边都要读到它）：
  * `脏表-*.xlsx`   —— 夹具本体（文件名带生成日期）
  * `脏表-答案卡.md` —— 预先算好的正确答案（每个数带 `sheet!单元格`）
                       [!] 对账靠它，不许靠印象。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "workspace"
XLSX = OUT / "脏表-2026-10-03.xlsx"
CARD = OUT / "脏表-答案卡.md"

# 三张表的明细数据（同时喂给答案卡）
#
# [!] 造数时必须避开三类"意外脏" —— 它们不是设计好的陷阱，
#   而是造数过程的副产物；对比实验里混进意外噪声会把结论搅浑：
#
#   ① 跨表整列重复：二季度的「上年同期」列 == 一季度的「实际」列、
#      二季度的「实际」列 == 三季度的「上年同期」列。独立评审会据此判定
#      「这是复制粘贴造的表、数据不可信」—— 判断完全正确。
#   ② 「上年同期」取的是上一季度的实际（业务上根本不成立）。
#   ③ 合计行把收入项和成本项混在一起（业务上很反常）。
#
#   -> 修法：科目统一成 5 个同类项（全是收入类，合计才自然）· 三张表的
#   计划/实际/上年 互不相等（`_assert_no_column_repeats` 盯着）·
#   上年同期取独立的合理值。
#
# 设计好的陷阱一个不少：表头不在第 1 行 · 行名不在 A 列 · 同 sheet 两张表 ·
#   纵向合并的分组标签 · 块尾合计行 · 「实际」紧挨「上年同期」· 二季度整列错位。
Q1 = [
    ("销售回款", 320.00, 331.50, 305.20),
    ("设备租赁",  88.00,  92.10,  95.40),
    ("技术服务",  45.00,  41.20,  38.60),
    ("仓储服务",  30.00,  33.80,  29.10),
    ("其他业务",  12.00,  10.40,  11.20),
]
Q7 = [   # 二季度（表头在第 3 行、整列错位、行名落在 C 列）
    ("销售回款", 340.00, 348.90, 326.80),
    ("设备租赁",  90.00,  86.40,  97.20),
    ("技术服务",  46.00,  48.70,  43.50),
    ("仓储服务",  31.00,  29.30,  34.10),
    ("其他业务",  13.00,  14.80,  12.40),
]
Q3 = [   # 三季度（纵向合并的分组标签：主营 / 其他）
    ("主营", "销售回款", 355.00, 362.40, 341.60),
    ("主营", "设备租赁",  92.00,  89.30,  88.50),
    ("主营", "技术服务",  47.00,  45.60,  46.10),
    ("其他", "仓储服务",  33.00,  35.20,  30.80),
    ("其他", "其他业务",  18.00,  16.20,  15.60),
]


def _assert_no_column_repeats():
    """修 ①：三张表的同一列不许跨表重复 —— 那会被读成"造表痕迹/数据不可信"。

    这不是洁癖：三路 bash-only 里两路独立把跨表重复列当成"数据可疑"的证据，
    并且据此降低了对整张表的信任。夹具里出现这种东西，测的就变成了
    "模型会不会被噪声带偏"，而不是我们想测的那几条。
    """
    def col(rows, idx):
        return [r[idx] for r in rows]
    pairs = [("Q1", Q1), ("Q7", Q7), ("Q3", Q3)]
    seen = {}
    for li, (name_i, rows_i) in enumerate(pairs):
        for idx, tag in ((1, "计划"), (2, "实际"), (3, "上年同期")):
            key = tuple(col(rows_i, idx))
            for name_j, _ in pairs[:li]:
                pass
            if key in seen:
                raise AssertionError("★ 跨表整列重复：%s 的「%s」列 == %s"
                                     % (name_i, tag, seen[key]))
            seen[key] = "%s 的「%s」列" % (name_i, tag)
HDR = ["项目", "计划", "实际", "上年同期", "完成率"]


def _hdr_row(ws, r, cols=HDR, start=1):
    for i, h in enumerate(cols):
        ws.cell(row=r, column=start + i, value=h)


def _style_header(ws, r, c1, c2):
    from openpyxl.styles import Font, Alignment
    for c in range(c1, c2 + 1):
        cell = ws.cell(row=r, column=c)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")


def build():
    _assert_no_column_repeats()      # 修 ①：跨表整列重复 = "造表痕迹"，会让 agent 不信数据
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    # 一季度：表头在第 5 行 · 行名在 B 列（A 列空）· 块尾有合计 · 下面还叠一张表
    ws = wb.active
    ws.title = "一季度"
    ws["A1"] = "某某公司 2026 年第一季度经营简报"
    ws["A1"].font = Font(bold=True, size=14)
    ws.merge_cells("A1:F1")
    ws["A2"] = "单位：万元；本表数据未经审计，仅供内部参考。"
    ws.merge_cells("A2:F2")
    ws["A4"] = "一、主要指标"
    ws["A4"].font = Font(bold=True)
    _hdr_row(ws, 5, HDR, start=2)          # 表头从 B 列开始 -> A 列整列空着
    _style_header(ws, 5, 2, 6)
    r = 6
    for name, plan, act, ly in Q1:         # 行名在 B 列，A 列不填
        ws.cell(row=r, column=2, value=name)
        ws.cell(row=r, column=3, value=plan)
        ws.cell(row=r, column=4, value=act)
        ws.cell(row=r, column=5, value=ly)
        ws.cell(row=r, column=6, value="=D%d/C%d" % (r, r))   # 公式列（有缓存值下面补）
        r += 1
    tot = r                                # 合计行（块尾）
    ws.cell(row=tot, column=2, value="合计")
    ws.cell(row=tot, column=2).font = Font(bold=True)
    for col in (3, 4, 5):
        L = get_column_letter(col)
        ws.cell(row=tot, column=col, value="=SUM(%s6:%s%d)" % (L, L, tot - 1))
    ws.cell(row=tot, column=6, value="=D%d/C%d" % (tot, tot))

    r2 = tot + 2                           # 空一行 -> 同一 sheet 里的第二张表
    ws.cell(row=r2, column=2, value="二、分品种产量（吨）").font = Font(bold=True)
    _hdr_row(ws, r2 + 1, ["品种", "计划", "实际", "上年同期"], start=2)
    _style_header(ws, r2 + 1, 2, 5)
    for i, (nm, plan, act, ly) in enumerate(
            [("草  鱼", 120, 132, 118), ("鲈  鱼", 45, 39, 52),
             ("黄颡鱼", 30, 33, 28), ("白  鲢", 210, 205, 214)]):
        rr = r2 + 2 + i
        ws.cell(row=rr, column=2, value=nm)
        ws.cell(row=rr, column=3, value=plan)
        ws.cell(row=rr, column=4, value=act)
        ws.cell(row=rr, column=5, value=ly)

    # 二季度：表头在第 3 行 · 行名在 C 列（A=序号, B 空）
    ws2 = wb.create_sheet("二季度")
    ws2["A1"] = "二季度经营数据"
    ws2["A1"].font = Font(bold=True, size=13)
    _hdr_row(ws2, 3, ["序号"] + HDR, start=1)     # 表头在第 3 行
    _style_header(ws2, 3, 1, 6)
    for i, (name, plan, act, ly) in enumerate(Q7):
        rr = 4 + i
        ws2.cell(row=rr, column=1, value=i + 1)   # A=序号
        ws2.cell(row=rr, column=3, value=name)    # 行名在 C 列
        ws2.cell(row=rr, column=4, value=plan)
        ws2.cell(row=rr, column=5, value=act)
        ws2.cell(row=rr, column=6, value=ly)
    t2 = 4 + len(Q7)
    ws2.cell(row=t2, column=3, value="合计").font = Font(bold=True)
    for col in (4, 5, 6):
        L = get_column_letter(col)
        ws2.cell(row=t2, column=col, value="=SUM(%s4:%s%d)" % (L, L, t2 - 1))

    # 三季度：纵向合并的分组标签（只在左上角）
    ws3 = wb.create_sheet("三季度")
    ws3["A1"] = "三季度经营数据"
    ws3["A1"].font = Font(bold=True, size=13)
    _hdr_row(ws3, 3, ["类别", "项目", "计划", "实际", "上年同期"], start=1)
    _style_header(ws3, 3, 1, 5)
    rr = 4
    for grp, name, plan, act, ly in Q3:
        ws3.cell(row=rr, column=1, value=grp)
        ws3.cell(row=rr, column=2, value=name)
        ws3.cell(row=rr, column=3, value=plan)
        ws3.cell(row=rr, column=4, value=act)
        ws3.cell(row=rr, column=5, value=ly)
        rr += 1
    ws3.merge_cells("A4:A6")   # 主营
    ws3.merge_cells("A7:A8")   # 其他
    ws3["A4"].alignment = Alignment(vertical="center")

    for w, col in ((22, "A"), (14, "B"), (10, "C"), (10, "D"), (12, "E"), (10, "F")):
        for s in wb.worksheets:
            s.column_dimensions[col].width = w

    OUT.mkdir(parents=True, exist_ok=True)
    wb.save(str(XLSX))
    print("[ok] 夹具 ->", XLSX)

    # 公式没有缓存值 -> 用 LibreOffice 重算一次（本仓库有那个工具）
    _recalc()
    _write_card()


def _recalc():
    """公式格 openpyxl 写进去没有缓存值 —— 用 `recalculate_excel_formulas` 补上。

    [!] 失败不能留 None：那会让"完成率列 / 合计行"两边都读不到数，
    变成夹具缺陷而不是脏点。-> 退回直接写算好的字面值。
    """
    ok = False
    try:
        sys.path.insert(0, str(ROOT))
        from skills.office_parser.excel_transform import recalculate_excel_formulas as rc
        r = rc(str(XLSX))
        ok = bool(r.get("success"))
        print("[ok] 公式重算:", ok, str(r.get("message", ""))[:80])
    except Exception as exc:  # noqa: BLE001
        print("[i] 公式重算走不通（%s）—— 退回字面值" % type(exc).__name__)

    if ok:
        return
    # [!] 从 python 数据算，不从格子算 —— 格子此时存的是公式串（`=D6/C6`）
    from openpyxl import load_workbook
    wb = load_workbook(str(XLSX))
    ws = wb["一季度"]
    for i, (_n, plan, act, _ly) in enumerate(Q1):
        ws.cell(row=6 + i, column=6, value=round(act / plan, 4))
    tot = 6 + len(Q1)
    ws.cell(row=tot, column=6, value=round(sum(a for _, _, a, _ in Q1)
                                           / sum(p for _, p, _, _ in Q1), 4))
    for col, idx in ((3, 1), (4, 2), (5, 3)):
        ws.cell(row=tot, column=col, value=round(sum(q[idx] for q in Q1), 2))
    ws2 = wb["二季度"]
    t2 = 4 + len(Q7)
    for col, idx in ((4, 1), (5, 2), (6, 3)):
        ws2.cell(row=t2, column=col, value=round(sum(q[idx] for q in Q7), 2))
    wb.save(str(XLSX))
    print("[ok] 已完成率/合计 写成字面值（夹具里不再有 None 或公式串）")


def _write_card():
    """答案卡：每个数都带 sheet!单元格 —— 对账靠它，不靠印象。"""
    def fmt(v):
        return f"{v:.2f}" if isinstance(v, float) else str(v)

    L = []
    L.append("# 脏表 答案卡 —— 2026-10-03\n")
    L.append("> 给框架能力对比实验用。**对账靠这张表，不许靠印象。**\n")
    L.append("> 夹具：`workspace/脏表-2026-10-03.xlsx`（3 张工作表，**布局各不相同**）\n")
    L.append("""
## 这九个问题，两边都该答对

| # | 问题 | 正确答案 | 出处 |
|---|---|---|---|""")
    q1_act = sum(a for _, _, a, _ in Q1)
    q1_ly = sum(y for _, _, _, y in Q1)
    q7_act = sum(a for _, _, a, _ in Q7)
    q3_act = sum(act for _grp, _nm, _plan, act, _ly in Q3)
    q7_rh = Q7[2]      # 技术服务
    q1_rh = Q1[0]      # 销售回款
    rows = [
        ("1", "一季度「销售回款」的实际值与上年同期",
         "%.2f / %.2f" % (q1_rh[2], q1_rh[3]), "一季度!D6 / 一季度!E6"),
        ("2", "一季度**块尾的「合计」**实际值", fmt(q1_act),
         "一季度!D%d（**是合计行，不是第 6 个科目**）" % (6 + len(Q1))),
        ("3", "一季度「合计」的**上年同期**合计", fmt(q1_ly),
         "一季度!E%d" % (6 + len(Q1))),
        ("4", "★★ **二季度整列错位** —— 数据比表头**右移一格**（设计好的陷阱）",
         "**实际在 `E` 列**", "二季度!B3:F3（表头）vs 二季度!C4:F4（数据）"),
        ("5", "★ 二季度「技术服务」实际值", "%.2f" % q7_rh[2],
         "二季度!E6（★ 照表头字面读会得 `D6=%.2f`，那是**计划**）" % q7_rh[1]),
        ("6", "★ 二季度行名在哪一列", "**C 列**（B 列整列空）", "二季度!C4:C8"),
        ("7", "★ 三季度「其他业务」属于哪个类别", "**其他**", "三季度!A7（合并区 `A7:A8` 只有左上角有值）"),
        ("8", "三季度「销售回款」实际值", "%.2f" % Q3[0][3], "三季度!D4"),
        ("9", "★ **一季度那个 sheet 里有几张表**", "**两张**（主要指标 / 分品种产量）",
         "一季度!A4 与 一季度!B%d" % (6 + len(Q1) + 2)),
    ]
    for a, b, c, d in rows:
        L.append(f"| {a} | {b} | **{c}** | `{d}` |")

    L.append("""
## 三个「陷阱题」（**答错不是算错，是"把给它的那一段当成了整张表"**）

| # | 问题 | 正确答案 | 为什么是陷阱 |
|---|---|---|---|
| A | 二季度的「合计」实际值是多少 | **""" + fmt(q7_act) + """** | 合计行**紧跟在数据行后面**，容易被当成一个新科目再算一遍 |
| B | 一季度第二块的「实际」能不能加进万元总额 | **不能** —— 那一块单位是**吨** | 跨量纲相加 |
| C | 三张表加起来「实际」是多少 | **""" + fmt(q1_act + q7_act + q3_act) + """** | 需要**三张表都读全、且二季度按 E 列读** |

## 评分只看三件（**不看文笔**）

1. 上面 9 + 3 条，**逐条判对错**
2. 报告里每个数**能不能对回单元格**（答案卡里给了出处）
3. ★ **它有没有对"没读到的部分"下结论** —— 说了"原表没有 / 只有 N 张 / 就这些"，
   而实际上有 —— **这一条单独记，且最重**

---

## [!] 夹具修订记录（2026-10-03）

第一版有**三处"意外脏"**（不是设计好的陷阱，是我造数时的产物）——
**三路 bash-only 实验里，两个 agent 独立把它们当成「造表痕迹、数据不可信」的证据**，
判断完全正确。已修：

| 修什么 | 为什么 |
|---|---|
| **跨表整列重复**（二季度「上年同期」== 一季度「实际」） | agent 会据此**降低对整张表的信任**，测的就变成"会不会被噪声带偏" |
| **「上年同期」取的是上一季度的实际** | 业务上不成立 |
| **合计行把收入项和成本项混在一起** | agent 原话：「业务上很反常」 |

-> 现在科目统一成 5 个同类项（全是收入类，合计才自然），三张表各列**互不相等**
（`_assert_no_column_repeats` 守着）。**设计好的陷阱一个不少。**
""")
    CARD.write_text("\n".join(L), encoding="utf-8")
    print("[ok] 答案卡 ->", CARD)


if __name__ == "__main__":
    build()
