# -*- coding: utf-8 -*-
"""测试用的最小 LSP 服务器（stdio + Content-Length 分帧）。

只回答本仓库 `core/code_intel.py` 会发的那些方法，数据是写死的 ——
它的作用是把协议这一层变成可测的，不模拟任何真实语言分析。

ASCII-only 输出；stdout 只用来发协议帧（任何多余的字都会把分帧弄坏）。
"""

import json
import sys


def _read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, val = line.partition(b":")
        headers[key.strip().lower()] = val.strip()
    n = int(headers.get(b"content-length", b"0"))
    if n <= 0:
        return None
    try:
        return json.loads(sys.stdin.buffer.read(n).decode("utf-8"))
    except ValueError:
        return None


def _send(obj):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(
        ("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii") + body)
    sys.stdout.buffer.flush()


def _push_diagnostics(uri):
    """每次文档变化都推一条错误 —— 固定第二行第一列。"""
    if not uri:
        return
    _send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
           "params": {"uri": uri, "diagnostics": [{
               "range": {"start": {"line": 1, "character": 0},
                         "end": {"line": 1, "character": 3}},
               "severity": 1,
               "message": "fake undefined name",
           }]}})


def _loc(uri, line, char=0):
    return {"uri": uri,
            "range": {"start": {"line": line, "character": char},
                      "end": {"line": line, "character": char + 3}}}


def main():
    opened = {}
    while True:
        msg = _read()
        if msg is None:
            return
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "exit":
            return
        if method == "initialized":
            continue

        if method == "textDocument/didOpen":
            doc = params.get("textDocument") or {}
            opened[doc.get("uri")] = doc.get("text") or ""
            _push_diagnostics(doc.get("uri"))
            continue
        if method == "textDocument/didChange":
            doc = params.get("textDocument") or {}
            changes = params.get("contentChanges") or []
            if changes:
                opened[doc.get("uri")] = changes[0].get("text") or ""
            _push_diagnostics(doc.get("uri"))
            continue

        if mid is None:
            continue

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid,
                   "result": {"capabilities": {"hoverProvider": True}}})
        elif method == "shutdown":
            _send({"jsonrpc": "2.0", "id": mid, "result": None})
        elif method == "textDocument/definition":
            uri = (params.get("textDocument") or {}).get("uri")
            _send({"jsonrpc": "2.0", "id": mid, "result": [_loc(uri, 1, 0)]})
        elif method == "textDocument/implementation":
            uri = (params.get("textDocument") or {}).get("uri")
            _send({"jsonrpc": "2.0", "id": mid, "result": [_loc(uri, 2, 4)]})
        elif method == "textDocument/prepareCallHierarchy":
            uri = (params.get("textDocument") or {}).get("uri")
            _send({"jsonrpc": "2.0", "id": mid, "result": [{
                "name": "alpha", "kind": 12, "uri": uri,
                "range": _loc(uri, 1)["range"],
                "selectionRange": _loc(uri, 1)["range"],
            }]})
        elif method in ("callHierarchy/incomingCalls",
                        "callHierarchy/outgoingCalls"):
            key = "from" if method.endswith("incomingCalls") else "to"
            name = "caller" if key == "from" else "callee"
            item = params.get("item") or {}
            uri = item.get("uri") or "file:///proj/a.py"
            _send({"jsonrpc": "2.0", "id": mid, "result": [{
                key: {"name": name, "kind": 12, "uri": uri,
                      "range": _loc(uri, 3)["range"],
                      "selectionRange": _loc(uri, 3)["range"]},
                ("fromRanges" if key == "from" else "toRanges"):
                    [_loc(uri, 3)["range"]],
            }]})
        elif method == "textDocument/references":
            uri = (params.get("textDocument") or {}).get("uri")
            _send({"jsonrpc": "2.0", "id": mid,
                   "result": [_loc(uri, 1, 0), _loc(uri, 4, 2)]})
        elif method == "textDocument/hover":
            pos = params.get("position") or {}
            _send({"jsonrpc": "2.0", "id": mid,
                   "result": {"contents": {
                       "kind": "markdown",
                       "value": "pos %d:%d" % (pos.get("line"),
                                               pos.get("character"))}}})
        elif method == "textDocument/documentSymbol":
            uri = (params.get("textDocument") or {}).get("uri")
            _send({"jsonrpc": "2.0", "id": mid, "result": [
                {"name": "alpha", "kind": 12,
                 "range": _loc(uri, 0)["range"],
                 "selectionRange": _loc(uri, 0)["range"]},
                {"name": "Beta", "kind": 5,
                 "range": _loc(uri, 2)["range"],
                 "selectionRange": _loc(uri, 2)["range"],
                 "children": [
                     {"name": "gamma", "kind": 6,
                      "range": _loc(uri, 3)["range"],
                      "selectionRange": _loc(uri, 3)["range"]},
                 ]},
            ]})
        elif method == "workspace/symbol":
            _send({"jsonrpc": "2.0", "id": mid, "result": [
                {"name": "alpha", "kind": 12,
                 "location": _loc("file:///proj/a.py", 1, 0)},
            ]})
        else:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "no such method"}})


if __name__ == "__main__":
    main()
