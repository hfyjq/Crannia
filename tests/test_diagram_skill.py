# -*- coding: utf-8 -*-
"""diagram skill 测试：布局 / SVG 白名单 / Mermaid 子集 / 工具回执 / 边界。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_PASS = 0
_FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print("  [ok] %s" % label)
    else:
        _FAIL += 1
        print("  [!!] %s  %s" % (label, detail))


def test_capability_and_dependency():
    print("\n-- 1. 依赖与能力 --")
    from skills.diagram import layout as L
    check("graph-layout 可用（本机）", L.available(), "")
    import importlib.metadata as md
    meta = md.metadata("graph-layout")
    check("★ 许可 MIT（与 Apache-2.0 兼容）",
          "MIT" in str(meta.get("License-Expression") or meta.get("License") or ""),
          str(meta.get("License-Expression")))


def test_layout_flowchart_and_mindmap():
    print("\n-- 2. 两种布局 --")
    from skills.diagram.layout import flowchart_layout, mindmap_layout
    nodes, edges = flowchart_layout(
        ["A", "B", "C", "D"], [("A", "B"), ("B", "C"), ("A", "D"), ("D", "C")])
    check("★ 4 节点都有坐标", len(nodes) == 4, str(nodes))
    pos = {n.id: (n.x, n.y) for n in nodes}
    check("分层正确：A 在 B 之上", pos["A"][1] < pos["B"][1], str(pos))
    check("分层正确：C 在 B 之下", pos["C"][1] > pos["B"][1], str(pos))
    check("边都映射回业务 id",
          all(e.source in pos and e.target in pos for e in edges))

    tree, tree_edges = mindmap_layout(
        ["r", "a", "b", "a1", "a2"],
        [("r", "a"), ("r", "b"), ("a", "a1"), ("a", "a2")], root="r",
        orientation="left-to-right")
    tp = {n.id: (n.x, n.y) for n in tree}
    check("★ 树布局分层：根 < 子 < 孙",
          tp["r"][0] < tp["a"][0] < tp["a1"][0], str(tp))
    check("★ 树布局连线条数与边数一致", len(tree_edges) == 4, str(tree_edges))
    check("★ 枝干不画箭头（arrow=False）",
          all(e.arrow is False for e in tree_edges), str(tree_edges))
    orphan_nodes, orphan_edges = mindmap_layout(["x", "y"], [("x", "y")],
                                                root="missing")
    check("root 不在列表时不崩", len(orphan_nodes) == 2 and len(orphan_edges) == 1,
          "%s %s" % (orphan_nodes, orphan_edges))


def _no_overlap(nodes) -> bool:
    """任意两个节点框不重叠（中心距在两个轴上至少一个超过节点尺寸）。"""
    from skills.diagram.layout import NODE_W, NODE_H
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            if abs(a.x - b.x) < NODE_W and abs(a.y - b.y) < NODE_H:
                return False
    return True


def test_layout_no_overlap():
    print("\n-- 2b. 布局间距：节点框不许叠 --")
    from skills.diagram.layout import flowchart_layout, mindmap_layout
    # 早先布局库默认兄弟间距 50px、而节点框 150px 宽，同层节点直接叠住。
    # 这条钉的是"间距按节点尺寸给"。
    nodes, _ = flowchart_layout(
        ["A", "B", "C", "D"], [("A", "B"), ("B", "C"), ("A", "D"), ("D", "C")])
    check("★ 流程图同层节点不重叠", _no_overlap(nodes),
          str([(n.id, n.x, n.y) for n in nodes]))
    wide = ["r"] + ["c%d" % i for i in range(8)]
    tree, _ = mindmap_layout(wide, [("r", c) for c in wide[1:]], root="r")
    check("★ 思维导图 8 个兄弟不重叠", _no_overlap(tree),
          str([(n.id, n.x, n.y) for n in tree[:3]]))
    deep = ["n%d" % i for i in range(6)]
    chain, _ = mindmap_layout(deep, list(zip(deep, deep[1:])), root="n0")
    check("★ 思维导图 6 层链条不重叠", _no_overlap(chain),
          str([(n.id, n.x, n.y) for n in chain]))


def test_svg_whitelist_and_escape():
    print("\n-- 3. SVG 只用白名单元素 --")
    from skills.diagram.layout import LayoutNode, LayoutEdge
    from skills.diagram.svg import render_svg
    ns = [LayoutNode("a", 0, 0), LayoutNode("b", 300, 0)]
    es = [LayoutEdge("a", "b")]
    svg = render_svg(ns, es, {"a": "开始", "b": "<脚本>\"x\" & y"}, title="T<&>")
    from xml.etree import ElementTree as ET
    root = ET.fromstring(svg)
    check("SVG 良构", root.tag.endswith("svg"), root.tag)
    allowed = {'svg', 'rect', 'circle', 'ellipse', 'line', 'path', 'polygon',
               'polyline', 'text', 'tspan', 'image', 'g', 'defs', 'use',
               'linearGradient', 'radialGradient', 'stop', 'title', 'desc', 'style'}
    used = {e.tag.split('}')[-1] for e in root.iter()}
    check("★ 所有元素都在 create_svg_slide 白名单内",
          used <= allowed, str(used - allowed))
    check("★ 没有 marker（白名单不含）", "marker" not in used, "")
    check("中文/尖括号转义正确", "&lt;脚本&gt;" in svg and "&amp;" in svg, svg[:200])
    # 跨平台字体：Linux 上没有 Microsoft YaHei，必须给候选栈（与 excel_transform 同口径）
    from skills.diagram.svg import FONT_STACK
    check("★ 字体栈含 Windows 与 Linux 候选",
          "Microsoft YaHei" in FONT_STACK
          and ("Noto Sans CJK SC" in FONT_STACK or "WenQuanYi Micro Hei" in FONT_STACK)
          and "sans-serif" in FONT_STACK, str(FONT_STACK))
    check("字体栈写进 SVG font-family", "Noto Sans CJK SC" in svg, "")
    # 真正过一次生产校验器
    from skills.office_parser.handler import _svg_validate
    ok, why = _svg_validate(svg)
    check("★★ 过 _svg_validate", ok, why)
    # 枝干（arrow=False）只出线、不出三角 —— 思维导图与流程图共用渲染
    svg2 = render_svg([LayoutNode("r", 0, 0), LayoutNode("c", 300, 0)],
                      [LayoutEdge("r", "c", arrow=False)], {"r": "根", "c": "子"})
    check("★ 无箭头边不画三角", "<polygon" not in svg2, svg2[:300])
    check("★ 无箭头边真的有线", svg2.count("<line") == 1,
          str(svg2.count("<line")))
    check("有箭头边同时出线与三角",
          svg.count('<polygon points=') == 1 and svg.count("<line") == 1,
          "%s %s" % (svg.count('<polygon points='), svg.count("<line")))


def test_mermaid_subset():
    print("\n-- 4. Mermaid 子集 --")
    from skills.diagram.mermaid import parse_mermaid, looks_like_mermaid
    check("识别 flowchart", looks_like_mermaid("flowchart TD\n A --> B"))
    check("不认 sequenceDiagram", not looks_like_mermaid("sequenceDiagram\n A->>B"))
    p = parse_mermaid("flowchart LR\n  A[开始] --> B{判断}\n  B -- 是 --> C[完]")
    check("★ 节点/边解析", len(p.ids) == 3 and len(p.edges) == 2, str(p.ids))
    check("形状识别 decision", p.kinds.get("B") == "decision", str(p.kinds))
    check("带说明的边", ("B", "C") in (p.edge_labels or {}), str(p.edge_labels))
    m = parse_mermaid("mindmap\n  项目\n    前端\n      UI\n    后端")
    check("★ 缩进建树", len(m.ids) == 4 and m.edges == [("项目", "前端"),
                                                    ("前端", "UI"), ("项目", "后端")],
          str(m.edges))
    for bad in ("sequenceDiagram\n A->>B", "flowchart TD\n 没有箭头"):
        try:
            parse_mermaid(bad)
            check("不支持的写法明确报错: " + bad[:20], False, "没报错")
        except ValueError:
            check("不支持的写法明确报错: " + bad[:20], True, "")


def test_tool_receipts():
    print("\n-- 5. 工具回执（端到端） --")
    # 隔离落点：工具默认写 core.workspace.primary_output_dir（真实工作区）。
    # 并发跑全量时，test_primary_runtime 会盯"生产 data/ 没被动过"——
    # 所以这里把落点临时改到 tempdir，测完还原，别把产物写进真实工作区。
    import core.workspace as W
    real_out = W.primary_output_dir
    tmp = tempfile.mkdtemp(prefix="diagram-test-")
    W.primary_output_dir = lambda: Path(tmp)
    try:
        _tool_receipts_body(tmp)
    finally:
        W.primary_output_dir = real_out


def _tool_receipts_body(tmp: str):
    from skills.diagram import handler as H
    # 渲染前要过路径分级（与 file_parser 同一套）。框架装载时会把
    # SecurityGuard 传给 init；这里直接给一个，允许目录 = 这个临时落点。
    from core.security import SecurityGuard
    H.init(SecurityGuard(allowed_dirs=[tmp], command_blacklist=[],
                         audit_log_path=str(Path(tmp) / "audit.log")))
    r = H.create_flowchart(
        nodes=[{"id": "A", "label": "开始", "kind": "start"},
               {"id": "B", "label": "完", "kind": "end"}],
        edges=[("A", "B")], title="测试", output_name="test_flow")
    check("★ create_flowchart 成功", r.get("success") is True, str(r))
    p = Path(r.get("path") or "")
    check("落工作区是 .svg", p.suffix == ".svg" and p.is_file(), str(p))
    check("默认格式 svg", r.get("format") == "svg", str(r.get("format")))
    r2 = H.create_mindmap(source="mindmap\n  根\n    子1\n    子2",
                          output_name="test_mind")
    check("★ create_mindmap 成功", r2.get("success") is True, str(r2))
    if r2.get("success"):
        mind_svg = Path(r2["path"]).read_text(encoding="utf-8")
        # 此前这里传的是空边表：节点画得出来，一根线都没有
        check("★★ 思维导图真有连线（2 条边 → 2 段线）",
              mind_svg.count("<line") == 2, str(mind_svg.count("<line")))
        check("★ 思维导图不画箭头三角",
              'fill="#64748b"' not in mind_svg, mind_svg[:300])
    flow_svg = p.read_text(encoding="utf-8")
    check("★ 流程图仍有箭头三角（arrow 默认 True）",
          'fill="#64748b"' in flow_svg, flow_svg[:300])
    # 负对照：错输入不静默
    check("空 nodes 报错", H.create_flowchart(nodes=[], edges=[]).get("success") is False)
    check("错 Mermaid 语法报错",
          H.create_flowchart(source="sequenceDiagram\n A->>B").get("success") is False)
    check("错 kind 不匹配报错",
          H.create_mindmap(source="flowchart TD\n A-->B").get("success") is False)
    # 树必须连通：断开的节点会被布局库摆到同一处、叠成一个方块。
    # 交一张看不出来的图比报错更坏，所以这两条必须报错且带路线。
    r_nolink = H.create_mindmap(nodes=[{"id": "a"}, {"id": "b"}], edges=[])
    check("★ 只给节点不给边 → 报错，不交叠图",
          r_nolink.get("success") is False
          and "连不到" in str(r_nolink.get("error")), str(r_nolink))
    r_lost = H.create_mindmap(nodes=[{"id": "a"}, {"id": "b"}],
                              edges=[("b", "a")])
    check("★ 边方向反了（子 → 父）→ 报错并点明方向",
          r_lost.get("success") is False
          and "父 → 子" in str(r_lost.get("error")), str(r_lost))
    r_badroot = H.create_mindmap(nodes=[{"id": "a"}, {"id": "b"}],
                                 edges=[("a", "b")], root="zzz")
    check("★ root 不存在 → 报错并列出可用 id",
          r_badroot.get("success") is False
          and "zzz" in str(r_badroot.get("error")), str(r_badroot))
    r_one = H.create_mindmap(nodes=[{"id": "a", "label": "甲"}],
                             output_name="test_one")
    check("单个节点仍是合法思维导图", r_one.get("success") is True, str(r_one))
    # PNG 转换
    r3 = H.render_diagram(str(p), "png", "test_flow")
    check("★ SVG→PNG 成功", r3.get("success") is True, str(r3))
    if r3.get("success"):
        check("PNG 真有字节", Path(r3["path"]).stat().st_size > 1000, str(r3))
        check("★ PNG 名字带时间戳（不静默覆盖同名产物）",
              "_" in Path(r3["path"]).stem[len("test_flow"):], str(r3["path"]))
    # 渲染读文件前必须过路径分级：.env 是 FORBIDDEN，连读都不许
    bad = Path(tmp) / ".env"
    bad.write_text("SECRET=1", encoding="utf-8")
    r4 = H.render_diagram(str(bad), "png")
    check("★ render_diagram 过路径守卫（.env 被拒）",
          r4.get("success") is False and "路径不允许读取" in str(r4.get("error")),
          str(r4))
    # 非 SVG 直接拒，别让 resvg 报一句看不懂的解析错
    txt = Path(tmp) / "not-svg.txt"
    txt.write_text("hello", encoding="utf-8")
    r5 = H.render_diagram(str(txt), "png")
    check("★ 非 .svg 文件明确拒",
          r5.get("success") is False and ".svg" in str(r5.get("error")), str(r5))
    # 规模上限：先拒，别让布局先跑完再撞 100 KB
    many = [{"id": "n%d" % i} for i in range(H.MAX_NODES + 1)]
    r6 = H.create_flowchart(nodes=many, edges=[])
    check("★ 节点超上限 → 报错并给路线",
          r6.get("success") is False and "几张图" in str(r6.get("error")), str(r6))
    # 边标签：Mermaid 的 -- 说明 --> 与结构化 label 都要真画出来
    r7 = H.create_flowchart(
        source="flowchart LR\n  A[开始] -->|是| B[完成]",
        output_name="test_label")
    check("★ 边标签渲染成功", r7.get("success") is True, str(r7))
    if r7.get("success"):
        label_svg = Path(r7["path"]).read_text(encoding="utf-8")
        check("★★ 边上的说明文字进了 SVG",
              ">是</text>" in label_svg, label_svg[:300])
    r8 = H.create_flowchart(
        nodes=[{"id": "A", "label": "开始"}, {"id": "B", "label": "完成"}],
        edges=[{"source": "A", "target": "B", "label": "下一步"}],
        output_name="test_label2")
    check("★ 结构化 edges 的 label 也渲染",
          r8.get("success") is True
          and ">下一步</text>" in Path(r8["path"]).read_text(encoding="utf-8"),
          str(r8))


def test_skill_wiring():
    print("\n-- 6. 技能接线 --")
    from core.skill_sources import is_builtin_skill
    check("★ 登记为内置技能", is_builtin_skill("diagram"), "")
    from core.skill_index import build_skill_index
    idx = str(build_skill_index())
    check("出现在技能索引", "diagram" in idx, "")
    check("索引说 3 个工具", "diagram (3 个工具)" in idx, idx[:200])


def main() -> int:
    print("=" * 62)
    print("Diagram skill: layout / SVG / Mermaid / receipts / wiring")
    print("=" * 62)
    for fn in (test_capability_and_dependency, test_layout_flowchart_and_mindmap,
               test_layout_no_overlap, test_svg_whitelist_and_escape,
               test_mermaid_subset, test_tool_receipts, test_skill_wiring):
        print("\n-- %s --" % fn.__name__)
        fn()
    print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
