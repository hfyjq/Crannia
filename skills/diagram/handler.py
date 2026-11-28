# -*- coding: utf-8 -*-
"""结构化绘图 skill —— 流程图 / 思维导图 → SVG（默认）→ PNG/PPTX。

## 为什么这样分层

1. 布局：graph-layout 0.5.0（MIT）一套提供 Sugiyama（分层流程图）与
   Reingold-Tilford（整齐树思维导图）。不引 Graphviz 二进制 / Node+Chromium / JVM。
2. 渲染：手写 SVG 字符串，只用 `create_svg_slide` 的白名单元素 ——
   箭头用 path 自画，不用 marker（不在白名单）。
3. 交付：SVG 落工作区（`core.workspace.primary_output_dir` 唯一口径），
   `image/svg+xml` 已在 artifacts mime 白名单；
   PNG 走已装的 resvg_py，PPTX 复用 `svgs_to_pptx`（可选）。

## 输入

- 结构化：`nodes` + `edges`（JSON），可靠、可校验
- Mermaid 文本子集：`source` 参数传 `flowchart TD ...` / `mindmap ...`
  ——只认常见写法，不调 mermaid-cli；不支持的写法明确报错

缺 graph-layout 时给可读提示（不崩、不假装成功）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from . import layout as L
from . import mermaid as M
from . import svg as S

__all__ = ["init", "create_flowchart", "create_mindmap", "render_diagram"]

#: 模型面对的上限。布局是 O(n log n)，但 100 KB 的 SVG 早就先撞墙了；
#: 这里拦在布局之前，免得先算三十秒再报一句"图太复杂"。
MAX_NODES = 200
MAX_EDGES = 400

_security = None


def init(security_guard=None) -> None:
    """装载钩子：只接安全核，不探测依赖、不写文件（每次调用时才查）。"""
    global _security
    _security = security_guard


# 内部

def _out_dir() -> Path:
    from core.workspace import primary_output_dir
    d = Path(str(primary_output_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _safe_name(name: str, default: str) -> str:
    s = re.sub(r'[^\w\u4e00-\u9fff-]+', "_", str(name or "").strip())
    return (s or default)[:60]


def _parse_nodes_edges(nodes: Any, edges: Any) -> tuple[list[str], list[tuple[str, str]],
                                                        dict[str, str], dict[str, str],
                                                        dict[tuple[str, str], str]]:
    """把 manifest 的 nodes/edges 数组规整为 (ids, edges, labels, kinds, 边标签)。"""
    out_ids: list[str] = []
    labels: dict[str, str] = {}
    kinds: dict[str, str] = {}
    for item in nodes or []:
        if isinstance(item, str):
            nid, label, kind = item, item, "rect"
        elif isinstance(item, dict):
            nid = str(item.get("id") or item.get("name") or "").strip()
            label = str(item.get("label") or item.get("text") or nid)
            kind = str(item.get("kind") or item.get("shape") or "rect")
        else:
            continue
        if not nid:
            continue
        if nid not in labels:
            out_ids.append(nid)
        labels[nid] = label
        kinds[nid] = kind
    out_edges: list[tuple[str, str]] = []
    edge_labels: dict[tuple[str, str], str] = {}
    for e in edges or []:
        note = ""
        if isinstance(e, (list, tuple)) and len(e) >= 2:
            s, t = str(e[0]), str(e[1])
            if len(e) >= 3 and isinstance(e[2], str):
                note = e[2].strip()
        elif isinstance(e, dict):
            s = str(e.get("source") or e.get("from") or "")
            t = str(e.get("target") or e.get("to") or "")
            note = str(e.get("label") or e.get("text") or "").strip()
        else:
            continue
        if not s or not t:
            continue
        for nid in (s, t):
            if nid not in labels:
                out_ids.append(nid)
                labels[nid] = nid
                kinds[nid] = "rect"
        out_edges.append((s, t))
        if note:
            edge_labels[(s, t)] = note
    return out_ids, out_edges, labels, kinds, edge_labels


def _too_big(ids: list[str], edges: list[tuple[str, str]]) -> dict | None:
    """节点/边数量上限。超了先拒，别让布局先跑三十秒再撞 100 KB。"""
    if len(ids) > MAX_NODES:
        return _err("节点太多（%d > %d）——请合并或拆分，分几张图交付"
                    % (len(ids), MAX_NODES))
    if len(edges) > MAX_EDGES:
        return _err("连线太多（%d > %d）——请精简关系或分几张图交付"
                    % (len(edges), MAX_EDGES))
    return None


def _write_svg(svg_text: str, name: str) -> dict:
    dest = _out_dir() / ("%s_%s.svg" % (_safe_name(name, "diagram"), _stamp()))
    dest.write_text(svg_text, encoding="utf-8")
    return {
        "success": True,
        "path": str(dest),
        "format": "svg",
        "bytes": dest.stat().st_size,
        "hint": ("SVG 已生成（默认格式，可直接交付/内联）。"
                 "用 deliver_file 发给用户；要 PNG/PPTX 调 render_diagram。"),
    }


def _err(msg: str) -> dict:
    return {"success": False, "error": msg}


def _orphans(ids: list[str], edges: list[tuple[str, str]], root: str) -> list[str]:
    """从 root 沿着边（父 → 子）走不到的节点。

    思维导图必须是一棵连通的树。断开的节点会被布局库"随便摆"到同一处，
    叠成一个看不出来的方块 —— 所以宁可报错，也不交一张假图。
    """
    children: dict[str, list[str]] = {}
    for s, t in edges:
        children.setdefault(s, []).append(t)
    seen = {root}
    queue = [root]
    while queue:
        cur = queue.pop()
        for nxt in children.get(cur, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return [n for n in ids if n not in seen]


def _source_or_nodes(source: str, nodes: Any, edges: Any, want: str) -> dict:
    """二选一：Mermaid 文本 或 结构化 nodes/edges。返回统一中间结构或 error。"""
    if str(source or "").strip():
        if not M.looks_like_mermaid(source):
            return _err("source 只接受 flowchart/graph/mindmap 开头的 Mermaid 子集；"
                        "其他图表请用 nodes/edges 传结构化数据")
        parsed = M.parse_mermaid(source)
        if parsed.kind != want:
            return _err("source 是 %s，但这个工具要 %s —— 请换工具或改用 nodes/edges"
                        % (parsed.kind, want))
        return {"parsed": parsed}
    ids, eds, labels, kinds, edge_labels = _parse_nodes_edges(nodes, edges)
    if not ids:
        return _err("至少给一个节点（nodes），或用 source 传 Mermaid 文本")
    return {"ids": ids, "edges": eds, "labels": labels, "kinds": kinds,
            "edge_labels": edge_labels}


# 工具

def create_flowchart(source: str = "", nodes: Any = None, edges: Any = None,
                     orientation: str = "top-to-bottom", title: str = "",
                     output_name: str = "flowchart") -> dict:
    """把节点+边画成分层流程图（默认 SVG）。支持 Mermaid flowchart 子集。"""
    if not L.available():
        return _err("缺少 `graph-layout`（MIT，约 3 MB）。"
                    "请 `pip install graph-layout` —— 不需要 Graphviz 系统程序。")
    try:
        data = _source_or_nodes(source, nodes, edges, "flowchart")
        if "error" in data:
            return data
        if "parsed" in data:
            p = data["parsed"]
            ids, eds, labels, kinds = p.ids, p.edges, p.labels, p.kinds
            edge_labels = p.edge_labels or {}
            orientation = p.orientation
        else:
            ids, eds, labels, kinds = (data["ids"], data["edges"],
                                       data["labels"], data["kinds"])
            edge_labels = data["edge_labels"]
        over = _too_big(ids, eds)
        if over:
            return over
        lay_nodes, lay_edges = L.flowchart_layout(ids, eds, orientation=orientation)
        svg = S.render_svg(lay_nodes, lay_edges, labels, kinds, title=title,
                           edge_labels=edge_labels)
        return _write_svg(svg, output_name)
    except Exception as exc:                        # noqa: BLE001
        return _err("%s: %s" % (type(exc).__name__, exc))


def create_mindmap(source: str = "", nodes: Any = None, edges: Any = None,
                   root: str = "", orientation: str = "left-to-right",
                   title: str = "", output_name: str = "mindmap") -> dict:
    """把树画成思维导图（整齐树布局，默认 SVG）。支持 Mermaid mindmap 子集。"""
    if not L.available():
        return _err("缺少 `graph-layout`（MIT，约 3 MB）。"
                    "请 `pip install graph-layout` —— 不需要 Graphviz 系统程序。")
    try:
        data = _source_or_nodes(source, nodes, edges, "mindmap")
        if "error" in data:
            return data
        if "parsed" in data:
            p = data["parsed"]
            ids, eds, labels, kinds = p.ids, p.edges, p.labels, p.kinds
            root = root or ids[0]
        else:
            ids, eds, labels, kinds = (data["ids"], data["edges"],
                                       data["labels"], data["kinds"])
            root = root or ids[0]
        if root not in labels:
            return _err("root「%s」不在节点里；可用的 id：%s"
                        % (root, "、".join(ids[:8])))
        over = _too_big(ids, eds)
        if over:
            return over
        lost = _orphans(ids, eds, root)
        if lost:
            return _err("这些节点连不到 root「%s」的树上：%s —— "
                        "补上它们到父节点的连线（边的方向是 父 → 子），"
                        "或改用 Mermaid mindmap 按缩进写（第一行是根）"
                        % (root, "、".join(lost[:8])))
        lay_nodes, lay_edges = L.mindmap_layout(ids, eds, root, orientation=orientation)
        svg = S.render_svg(lay_nodes, lay_edges, labels, kinds, title=title)
        return _write_svg(svg, output_name)
    except Exception as exc:                        # noqa: BLE001
        return _err("%s: %s" % (type(exc).__name__, exc))


def render_diagram(file_path: str, target: str = "png", output_name: str = "") -> dict:
    """把已有 SVG 转成 PNG 或 PPTX。默认 SVG 不需调用本工具。"""
    if _security is None:
        return _err("安全核未接线：渲染读文件前必须过路径分级，"
                    "请确认 skill 是通过框架装载的")
    try:
        p = Path(str(_security.validate_path(str(file_path or ""), "read")))
    except Exception as exc:                        # noqa: BLE001
        return _err("路径不允许读取：%s: %s" % (type(exc).__name__, exc))
    if not p.is_file():
        return _err("文件不存在：%s" % p)
    if p.suffix.lower() != ".svg":
        return _err("只接受 .svg 文件（拿到的是 %s）" % (p.suffix or "无扩展名"))
    fmt = str(target or "png").lower().lstrip(".")
    if fmt not in ("png", "pptx"):
        return _err("target 只能是 png / pptx（默认 SVG 直接用，不用转）")
    try:
        if fmt == "png":
            # resvg_py 已经在框架里（svgs_to_pptx 用它栅格化）；
            # 它读文件字节，不走 PIL（PIL 不认 SVG）
            import resvg_py
            # 用 svg_path（与 office_parser 的 pptx 导出同款），
            # resvg_py 的 svg_string 参数名不是 svg——别猜。
            # font_family 给 resvg 一层兜底：Linux 上没有 Microsoft YaHei，
            # SVG 里的 font-family 栈会挑第一个存在的，这里再显式传一次。
            from .svg import FONT_STACK
            png = resvg_py.svg_to_bytes(
                svg_path=str(p), font_family=", ".join(FONT_STACK))
            dest = _out_dir() / ("%s_%s.%s" % (
                _safe_name(output_name or p.stem + "_图", "diagram"),
                _stamp(), "png"))
            dest.write_bytes(png)
            return {"success": True, "path": str(dest), "format": "png",
                    "bytes": dest.stat().st_size,
                    "hint": "PNG 已生成。用 deliver_file 发给用户。"}
        # pptx：复用 office_parser 的 svgs_to_pptx 前提是要建 PPT 工程；
        # 这里给明确指引而不是半吊子实现
        return _err("PPTX 请用 office_parser 的 `svgs_to_pptx`（把 SVG 放进"
                    " slide 序列再转）；单张图建议直接用 SVG 或 PNG")
    except Exception as exc:                        # noqa: BLE001
        return _err("%s: %s" % (type(exc).__name__, exc))
