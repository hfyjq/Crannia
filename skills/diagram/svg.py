# -*- coding: utf-8 -*-
"""结构化数据 → SVG 字符串（只用 create_svg_slide 的白名单元素）。

白名单：svg rect circle ellipse line path polygon polyline text tspan
image g defs use linearGradient radialGradient stop title desc style。
所以不能用 marker/filter/clipPath；箭头用 path 画三角；
阴影用半透明偏移矩形近似；圆角用 rect rx。

中文：不依赖字体测量（SVG 由浏览器渲染），但标签要 xml 转义；
多行用 tspan 手工折行（按字符宽粗估，避免溢出节点框）。
"""

from __future__ import annotations

import math
from xml.sax.saxutils import escape, quoteattr
from typing import Iterable

from .layout import LayoutEdge, LayoutNode, NODE_W, NODE_H

__all__ = ["render_svg", "MAX_SVG_BYTES"]

MAX_SVG_BYTES = 100 * 1024          # 与 _svg_validate 一致

_NODE_W = NODE_W
_NODE_H = NODE_H
_PAD = 40.0

_PALETTE = (
    "#eef4ff", "#eafaf1", "#fff4e6", "#f3e8ff", "#ffe8ec",
    "#e6f7fa", "#f5f0e6", "#e8f0fe",
)
_STROKE = "#334155"
_TEXT = "#0f172a"
_EDGE = "#64748b"

#: CJK 字体候选（与 excel_transform.py:53-58 同一份口径：Windows 在前，mac/linux 在后）。
#: 写进 SVG 的 font-family 让浏览器/resvg 自己挑第一个存在的——
#: 不能写死 "Microsoft YaHei"：Linux 上没有它，会出一片豆腐块。
FONT_STACK = ("Microsoft YaHei", "SimHei", "SimSun", "DengXian", "KaiTi",
              "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC",
              "Source Han Sans SC", "WenQuanYi Micro Hei", "Arial Unicode MS",
              "sans-serif")


def _t(label: str) -> str:
    return escape(str(label), {'"': "&quot;"})


def _wrap(text: str, width: int = 14) -> list[str]:
    """粗估折行：CJK 按 1 字宽、ASCII 按 0.55 字宽。"""
    out, cur, w = [], "", 0.0
    for ch in str(text):
        cw = 1.0 if ord(ch) > 0x2E80 else 0.55
        if w + cw > width and cur:
            out.append(cur)
            cur, w = "", 0.0
        cur += ch
        w += cw
    if cur:
        out.append(cur)
    return out or [""]


def _node_shape(kind: str) -> str:
    return {
        "start": "ellipse", "end": "ellipse",
        "decision": "polygon", "rect": "rect", "default": "rect",
    }.get(str(kind or "").lower(), "rect")


def _shape_xml(x: float, y: float, kind: str, fill: str) -> str:
    cx, cy = x, y
    k = _node_shape(kind)
    if k == "ellipse":
        return ('<ellipse cx="%.1f" cy="%.1f" rx="%.1f" ry="%.1f" '
                'fill="%s" stroke="%s" stroke-width="1.6"/>'
                % (cx, cy, _NODE_W / 2, _NODE_H / 2, fill, _STROKE))
    if k == "polygon":
        pts = ("%.1f,%.1f %.1f,%.1f %.1f,%.1f %.1f,%.1f" % (
            cx, cy - _NODE_H / 2, cx + _NODE_W / 2, cy,
            cx, cy + _NODE_H / 2, cx - _NODE_W / 2, cy))
        return ('<polygon points="%s" fill="%s" stroke="%s" stroke-width="1.6"/>'
                % (pts, fill, _STROKE))
    return ('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="10" '
            'fill="%s" stroke="%s" stroke-width="1.6"/>'
            % (x - _NODE_W / 2, y - _NODE_H / 2, _NODE_W, _NODE_H, fill, _STROKE))


def _border(cx: float, cy: float, fx: float, fy: float) -> tuple[float, float]:
    """节点 (cx,cy) 边框上、朝着 (fx,fy) 的那个点。

    按矩形求交（`min(半宽/|ux|, 半高/|uy|)`）；椭圆与菱形是近似，
    差的那点边长在 150x56 的框上看不出来。
    """
    dx, dy = cx - fx, cy - fy
    dist = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dist, dy / dist
    tx = (_NODE_W / 2) / abs(ux) if abs(ux) > 1e-9 else float("inf")
    ty = (_NODE_H / 2) / abs(uy) if abs(uy) > 1e-9 else float("inf")
    t = min(tx, ty, dist)
    return cx - ux * t, cy - uy * t


def _seg(x1: float, y1: float, x2: float, y2: float) -> str:
    return ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
            'stroke-width="1.8"/>' % (x1, y1, x2, y2, _EDGE))


def _arrow(x1: float, y1: float, x2: float, y2: float, *,
           clip_start: bool = True) -> str:
    """从 (x1,y1) 指向 (x2,y2) 的线段 + 自画三角（marker 不在白名单）。

    `clip_start=False` 用于折线：起点是上一个拐点，不是节点中心，
    不该按节点半宽往里缩。
    """
    sx, sy = _border(x1, y1, x2, y2) if clip_start else (x1, y1)
    ex, ey = _border(x2, y2, x1, y1)
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy) or 1.0
    ux, uy = dx / dist, dy / dist
    parts = [_seg(sx, sy, ex, ey)]
    # 三角：尖端在 (ex,ey)，两翼后撤 10px 并张开 5px
    bx, by = ex - ux * 10, ey - uy * 10
    px, py = -uy, ux
    pts = "%.1f,%.1f %.1f,%.1f %.1f,%.1f" % (
        ex, ey, bx + px * 5, by + py * 5, bx - px * 5, by - py * 5)
    parts.append('<polygon points="%s" fill="%s"/>' % (pts, _EDGE))
    return "".join(parts)


def _plain(x1: float, y1: float, x2: float, y2: float, *,
           clip_start: bool = True) -> str:
    """无箭头的连线（思维导图的枝干）。两端各缩到节点边框外沿。"""
    sx, sy = _border(x1, y1, x2, y2) if clip_start else (x1, y1)
    ex, ey = _border(x2, y2, x1, y1)
    return _seg(sx, sy, ex, ey)


def _label_width(text: str) -> float:
    """粗估一行字的宽度：CJK 按 12px、ASCII 按 7px（font-size 12）。"""
    return sum(12.0 if ord(ch) > 0x2E80 else 7.0 for ch in str(text))


def _edge_label(text: str, x1: float, y1: float, x2: float, y2: float) -> str:
    """连线中点上的说明文字，垫一块白底避免被线穿过。"""
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy) or 1.0
    mx = (x1 + x2) / 2 + (-dy / dist) * 10
    my = (y1 + y2) / 2 + (dx / dist) * 10
    w = _label_width(text) + 10
    return ('<rect x="%.1f" y="%.1f" width="%.1f" height="17" rx="3" '
            'fill="#ffffff" opacity="0.9"/>'
            '<text x="%.1f" y="%.1f" text-anchor="middle" font-size="12" '
            'font-family=%s fill="%s">%s</text>'
            % (mx - w / 2, my - 13, w, mx, my - 1,
               quoteattr(", ".join(FONT_STACK)), _EDGE, _t(text)))


def render_svg(nodes: list[LayoutNode], edges: list[LayoutEdge],
               labels: dict[str, str], kinds: dict[str, str] | None = None,
               *, title: str = "", width: int = 0, height: int = 0,
               edge_labels: dict[tuple[str, str], str] | None = None) -> str:
    """生成 SVG 文本（默认调用方直接落盘为 .svg）。"""
    kinds = kinds or {}
    all_x = [n.x for n in nodes] or [0.0]
    all_y = [n.y for n in nodes] or [0.0]
    w = int(width or (max(all_x) - min(all_x) + _NODE_W + _PAD * 2))
    h = int(height or (max(all_y) - min(all_y) + _NODE_H + _PAD * 2))
    ox, oy = _PAD - min(all_x) + _NODE_W / 2, _PAD - min(all_y) + _NODE_H / 2
    pos = {n.id: (n.x + ox, n.y + oy) for n in nodes}

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" '
           'viewBox="0 0 %d %d" width="%d" height="%d">' % (w, h, w, h)]
    if title:
        out.append('<title>%s</title>' % _t(title))
    out.append('<rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>' % (w, h))
    # 边先画，避免盖住节点
    for e in edges:
        if e.source not in pos or e.target not in pos:
            continue
        x1, y1 = pos[e.source]
        x2, y2 = pos[e.target]
        if e.points:
            prev = (x1, y1)
            for px, py in e.points:
                cur = (px + ox, py + oy)
                out.append(_seg(prev[0], prev[1], cur[0], cur[1]))
                prev = cur
            if e.arrow:
                out.append(_arrow(prev[0], prev[1], x2, y2, clip_start=False))
            else:
                out.append(_plain(prev[0], prev[1], x2, y2, clip_start=False))
        elif e.arrow:
            out.append(_arrow(x1, y1, x2, y2))
        else:
            out.append(_plain(x1, y1, x2, y2))
    # 节点
    for i, n in enumerate(nodes):
        x, y = pos[n.id]
        fill = _PALETTE[i % len(_PALETTE)]
        out.append(_shape_xml(x, y, kinds.get(n.id, "rect"), fill))
        lines = _wrap(labels.get(n.id, n.id))
        start_y = y - (len(lines) - 1) * 8.0
        for j, line in enumerate(lines):
            out.append('<text x="%.1f" y="%.1f" text-anchor="middle" '
                       'font-size="14" font-family=%s fill="%s">%s</text>'
                       % (x, start_y + j * 16 + 5,
                          quoteattr(", ".join(FONT_STACK)), _TEXT, _t(line)))
    # 边上的说明文字最后画，压在线与节点之上（垫白底）
    for e in edges:
        text = (edge_labels or {}).get((e.source, e.target))
        if not text or e.source not in pos or e.target not in pos:
            continue
        x1, y1 = pos[e.source]
        x2, y2 = pos[e.target]
        out.append(_edge_label(text, x1, y1, x2, y2))
    out.append('</svg>')
    svg = "\n".join(out)
    if len(svg.encode("utf-8")) > MAX_SVG_BYTES:
        raise ValueError("图太复杂（SVG 超过 100 KB）——请减少节点或简化文字")
    return svg
