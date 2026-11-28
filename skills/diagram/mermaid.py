# -*- coding: utf-8 -*-
"""Mermaid 文本子集解析 —— 只认常见写法，不引 mermaid-cli（Node+Chromium）。

支持：
  flowchart TD / LR / BT / RL   （graph 同义）
    A[文字] --> B[文字]
    A -- 说明 --> B
    A[文字] -->|说明| B
    A{判断} --> C
    A([起止]) --> B
  mindmap
    根
      子1
        孙1
      子2

不支持的写法一律明确报错，不静默画错。
id 用原名（中文也行）；方括号/花括号里的文字当 label。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ParsedDiagram", "parse_mermaid", "looks_like_mermaid"]


@dataclass
class ParsedDiagram:
    kind: str                              # "flowchart" | "mindmap"
    ids: list[str]
    edges: list[tuple[str, str]]
    labels: dict[str, str]
    kinds: dict[str, str]
    orientation: str = "top-to-bottom"
    edge_labels: dict[tuple[str, str], str] | None = None


_NODE_RE = re.compile(r'^\s*([^\s\[\]{}()]+)\s*'
                      r'(?:\[([^\]]*)\]|\{([^}]*)\}|\(\(([^)]*)\)\))?\s*$')


def looks_like_mermaid(text: str) -> bool:
    head = str(text or "").lstrip().lower()
    return head.startswith(("flowchart ", "graph ", "mindmap"))


def _parse_node(token: str) -> tuple[str, str, str]:
    """'A[开始]' -> ('A','开始','rect')；'B{判断}' -> kind=decision。"""
    token = token.strip()
    m = _NODE_RE.match(token)
    if not m:
        # 纯中文/带空格的 id（mindmap 里常见）当 label
        return token, token, "rect"
    nid, square, curly, round_ = m.groups()
    if curly is not None:
        return nid, curly, "decision"
    if round_ is not None:
        return nid, round_, "start"
    if square is not None:
        return nid, square, "rect"
    return nid, nid, "rect"


def _orientation(word: str) -> str:
    return {
        "TD": "top-to-bottom", "TB": "top-to-bottom",
        "BT": "bottom-to-top", "LR": "left-to-right", "RL": "right-to-left",
    }.get(str(word or "").upper(), "top-to-bottom")


def parse_mermaid(text: str) -> ParsedDiagram:
    lines = [ln.rstrip() for ln in str(text or "").splitlines()
             if ln.strip() and not ln.lstrip().startswith("%%")]
    if not lines:
        raise ValueError("Mermaid 文本是空的")
    head = lines[0].strip().lower()
    if head.startswith(("flowchart ", "graph ")):
        return _flow(lines)
    if head == "mindmap":
        return _mind(lines)
    raise ValueError("只支持 flowchart/graph 与 mindmap 开头；"
                     "sequenceDiagram/classDiagram 等不在子集内")


def _flow(lines: list[str]) -> ParsedDiagram:
    orient = _orientation(lines[0].split(None, 1)[1] if len(lines[0].split(None, 1)) > 1 else "TD")
    ids: list[str] = []
    labels: dict[str, str] = {}
    kinds: dict[str, str] = {}
    edges: list[tuple[str, str]] = []
    edge_labels: dict[tuple[str, str], str] = {}

    def add(nid: str, label: str, kind: str) -> None:
        if nid not in labels:
            ids.append(nid)
            labels[nid] = label or nid
            kinds[nid] = kind
        else:
            # 同一个节点在另一条边里可能只写裸 id（B 而不是 B{判断}）。
            # 这种情况别覆盖首次登记的形状/标签，否则 decision 会被冲成 rect。
            if label and label != nid:
                labels[nid] = label
            if kind != "rect":
                kinds[nid] = kind

    for raw in lines[1:]:
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        # A -- 说明 --> B / A -->|说明| B / A --> B
        m = re.match(r'^(.+?)\s*--\s*(.+?)\s*-->\s*(.+)$', line)
        if m:
            left, note, right = m.groups()
        else:
            m = re.match(r'^(.+?)\s*-->\s*\|([^|]*)\|\s*(.+)$', line)
            if m:
                left, note, right = m.groups()
            else:
                m = re.match(r'^(.+?)\s*-->\s*(.+)$', line)
                if not m:
                    raise ValueError("看不懂这一行（Mermaid 子集）：%s" % line[:80])
                left, right, note = m.group(1), m.group(2), ""
        s, sl, sk = _parse_node(left)
        t, tl, tk = _parse_node(right)
        add(s, sl, sk)
        add(t, tl, tk)
        edges.append((s, t))
        if note:
            edge_labels[(s, t)] = note.strip()
    if not edges:
        raise ValueError("flowchart 里没找到任何 `-->` 连线")
    return ParsedDiagram("flowchart", ids, edges, labels, kinds,
                         orientation=orient, edge_labels=edge_labels)


def _mind(lines: list[str]) -> ParsedDiagram:
    """按缩进层次建树：缩进深的挂到最近一层缩进浅的节点。"""
    stack: list[tuple[int, str]] = []
    ids: list[str] = []
    labels: dict[str, str] = {}
    kinds: dict[str, str] = {}
    edges: list[tuple[str, str]] = []
    root = ""
    for raw in lines[1:]:
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        nid, label, _kind = _parse_node(raw.strip())
        # 同名节点加序号，避免覆盖
        base, n = nid, 1
        while nid in labels:
            n += 1
            nid = "%s_%d" % (base, n)
        labels[nid] = label
        kinds[nid] = "rect"
        ids.append(nid)
        if not root:
            root = nid
            kinds[nid] = "start"
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            edges.append((stack[-1][1], nid))
        stack.append((indent, nid))
    if not ids:
        raise ValueError("mindmap 里没找到任何节点")
    return ParsedDiagram("mindmap", ids, edges, labels, kinds,
                         orientation="left-to-right")
