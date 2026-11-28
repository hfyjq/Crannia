# -*- coding: utf-8 -*-
"""布局适配层 —— 把"节点/边"喂给 graph-layout，取回可画 SVG 的坐标。

为什么不是自研：
  流程图要分层（Sugiyama），思维导图要整齐树（Reingold-Tilford）。
  graph-layout 0.5.0（MIT）一套覆盖两种，wheel 自带 Cython 加速、
  缺加速时回退纯 Python —— 不引 Graphviz 二进制、不引 Node/Chromium。

依赖缺失时 `available()` 返回 False，工具层给可读提示（不崩、不假装成功）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["available", "LayoutNode", "LayoutEdge", "flowchart_layout",
           "mindmap_layout", "NODE_W", "NODE_H"]

#: 渲染层节点框的尺寸。布局的间距按它算，渲染从这一处取 —— 两边各写一份
#: 就会出现"框 150 宽、间距按 50 排"的叠图（本文件早先那版就是这样）。
NODE_W = 150.0
NODE_H = 56.0

#: 兄弟 / 层间距：节点框加一点留白。
_NODE_GAP = NODE_W + 40.0
_LAYER_GAP = NODE_H + 40.0


@dataclass(frozen=True)
class LayoutNode:
    id: str
    x: float
    y: float


@dataclass(frozen=True)
class LayoutEdge:
    source: str
    target: str
    points: tuple[tuple[float, float], ...] = ()
    #: 连线画不画箭头。思维导图的枝干是"归属"不是"流向"，
    #: 与流程图共用一套渲染，靠这个字段分。
    arrow: bool = True


def available() -> bool:
    try:
        import graph_layout  # noqa: F401
        return True
    except Exception:                              # noqa: BLE001
        return False


def _require():
    if not available():
        raise RuntimeError(
            "缺少 `graph-layout`（布局库）。请先 `pip install graph-layout`"
            "（MIT，约 3 MB，不需要 Graphviz 系统程序）。")
    from graph_layout import SugiyamaLayout, ReingoldTilfordLayout
    return SugiyamaLayout, ReingoldTilfordLayout


def _size_for(count: int, orientation: str) -> tuple[float, float]:
    """画布尺寸：给节点数留够空间，避免挤成一团。"""
    span = max(600.0, min(2400.0, 180.0 * max(1, count)))
    if orientation in ("left-to-right", "right-to-left"):
        return (span, 900.0)
    return (900.0, span)


def flowchart_layout(ids: list[str], edges: list[tuple[str, str]], *,
                     orientation: str = "top-to-bottom") -> tuple[list[LayoutNode], list[LayoutEdge]]:
    """分层布局：适合流程图 / 时序 / 依赖图。

    ids 是节点业务 id 的有序列表；edges 是 (source_id, target_id)。
    返回带业务 id 的坐标——graph-layout 内部只给 index，这里映射回来。
    """
    SugiyamaLayout, _ = _require()
    if orientation not in ("top-to-bottom", "bottom-to-top",
                           "left-to-right", "right-to-left"):
        orientation = "top-to-bottom"
    index = {nid: i for i, nid in enumerate(ids)}
    nodes = [{"id": i} for i in range(len(ids))]
    links = [{"source": index[s], "target": index[t]}
             for s, t in edges if s in index and t in index]
    w, h = _size_for(len(ids), orientation)
    lay = SugiyamaLayout(nodes=nodes, links=links, size=(w, h),
                         orientation=orientation, crossing_iterations=12,
                         node_separation=_NODE_GAP,
                         layer_separation=_LAYER_GAP)
    lay.run()
    out = []
    for n in lay.nodes:                       # Node(index=.., x=.., y=..)
        if 0 <= n.index < len(ids):
            out.append(LayoutNode(ids[n.index], float(n.x), float(n.y)))
    bends = getattr(lay, "edge_bends", None) or {}
    out_edges = []
    for s, t in edges:
        if s not in index or t not in index:
            continue
        pts = tuple((float(p[0]), float(p[1]))
                    for p in (bends.get((index[s], index[t])) or []))
        out_edges.append(LayoutEdge(s, t, pts))
    return out, out_edges


def _tree_shape(ids: list[str], edges: list[tuple[str, str]], root: str
                ) -> tuple[int, int]:
    """树的层数与最宽那一层的节点数。

    Reingold-Tilford 把节点按给定画布铺开，而画布一旦写死，层数和宽度都
    随节点数变 —— 12 个兄弟塞进 900 高就贴在一起。所以画布按树的形状给。
    """
    children: dict[str, list[str]] = {}
    for s, t in edges:
        children.setdefault(s, []).append(t)
    seen = {root}
    frontier = [root]
    depth = 0
    level_max = 1
    while frontier:
        nxt: list[str] = []
        for cur in frontier:
            for c in children.get(cur, ()):
                if c not in seen:
                    seen.add(c)
                    nxt.append(c)
        if not nxt:
            break
        depth += 1
        level_max = max(level_max, len(nxt))
        frontier = nxt
    return depth, level_max


def mindmap_layout(ids: list[str], edges: list[tuple[str, str]], root: str, *,
                   orientation: str = "left-to-right"
                   ) -> tuple[list[LayoutNode], list[LayoutEdge]]:
    """整齐树布局：适合思维导图 / 组织架构 / 目录树。

    root 必须是 ids 里的一个；不在时退回第一个节点（不崩）。
    返回 (节点坐标, 连线) —— 连线与 flowchart_layout 同一个形状，
    `arrow=False`（枝干不画箭头）。
    """
    _, ReingoldTilfordLayout = _require()
    if not ids:
        return [], []
    if orientation not in ("top-to-bottom", "bottom-to-top",
                           "left-to-right", "right-to-left"):
        orientation = "left-to-right"
    index = {nid: i for i, nid in enumerate(ids)}
    nodes = [{"id": i} for i in range(len(ids))]
    links = [{"source": index[s], "target": index[t]}
             for s, t in edges if s in index and t in index]
    root_i = index.get(root, 0)
    depth, level_max = _tree_shape(ids, edges, root)
    if orientation in ("left-to-right", "right-to-left"):
        # 层沿 x 铺、兄弟沿 y 铺
        w = min(6000.0, max(600.0, _NODE_GAP * (depth + 1)))
        h = min(6000.0, max(360.0, _LAYER_GAP * (level_max + 1)))
    else:
        w = min(6000.0, max(360.0, _NODE_GAP * (level_max + 1)))
        h = min(6000.0, max(600.0, _LAYER_GAP * (depth + 1)))
    lay = ReingoldTilfordLayout(nodes=nodes, links=links, root=root_i,
                                size=(w, h), orientation=orientation)
    lay.run()
    out: list[LayoutNode] = []
    for n in lay.nodes:
        if 0 <= n.index < len(ids):
            out.append(LayoutNode(ids[n.index], float(n.x), float(n.y)))
    out_edges = [LayoutEdge(s, t, arrow=False)
                 for s, t in edges if s in index and t in index]
    return out, out_edges
