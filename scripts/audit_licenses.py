#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""审计一个安装树里的第三方许可。

    .venv/Scripts/python.exe scripts/audit_licenses.py                 # 默认扫 .venv
    .venv/Scripts/python.exe scripts/audit_licenses.py <dir>           # 扫指定 site-packages
    .venv/Scripts/python.exe scripts/audit_licenses.py <dir> --flag    # 只列 copyleft 家族
    .venv/Scripts/python.exe scripts/audit_licenses.py <dir> --json
    .venv/Scripts/python.exe scripts/audit_licenses.py --all           # 生成两份 SBOM（扫随包 runtime）
    .venv/Scripts/python.exe scripts/audit_licenses.py --check         # 与仓里那两份逐字对账

判据只认 wheel 自带的元数据（`*.dist-info/METADATA`），不凭记忆：
依次读 `License-Expression`（PEP 639）→ `Classifier: License ::` → `License`。
三样都没有，记 UNKNOWN，不猜（读不到不是 0）。

注意：它只做归类，不做法律判断：同一个 LGPL 包，动态链接 + 随附许可/源码是合规的，
静态打包进闭源二进制才需要另议。判读在 `THIRD-PARTY.md`。
"""

from __future__ import annotations

import json
import re
import sys
from email.parser import Parser
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

#: 需要人看一眼的家族（顺序 = 严重度）。匹配在规范化后的标签上做。
_FAMILIES = (
    ("AGPL", re.compile(r"\bAGPL\b|AFFERO", re.I)),
    ("GPL", re.compile(r"\bGPL\b|(?<!LESSER )GENERAL PUBLIC LICENSE", re.I)),
    ("LGPL", re.compile(r"\bLGPL\b|LESSER GENERAL PUBLIC", re.I)),
    ("MPL", re.compile(r"\bMPL\b|MOZILLA PUBLIC", re.I)),
    ("EPL", re.compile(r"\bEPL\b|ECLIPSE PUBLIC", re.I)),
    ("CDDL", re.compile(r"\bCDDL\b", re.I)),
    ("SSPL", re.compile(r"\bSSPL\b|SERVER SIDE PUBLIC", re.I)),
    ("BUSL", re.compile(r"\bBUSL\b|BUSINESS SOURCE", re.I)),
    ("CC-BY-SA", re.compile(r"CC[- ]BY[- ]SA|SHAREALIKE", re.I)),
    ("EUPL", re.compile(r"\bEUPL\b", re.I)),
    ("NC", re.compile(r"NON[- ]?COMMERCIAL|NONCOMMERCIAL", re.I)),
)

_PERMISSIVE = re.compile(
    r"MIT|BSD|APACHE|ISC|PSF|PYTHON SOFTWARE|UNLICENSE|CC0|ZLIB|"
    r"PUBLIC DOMAIN|HPND|MIT-CMU|ARTISTIC|BLUE OAK|UNICODE|X11",
    re.I,
)

#: 不是 Python 包、但确实随安装包分发的东西。
#:
#: 为什么手写而不是扫：它们的版本不在 `dist-info` 里 —— CPython 与 Tcl/Tk 在
#: `runtime.json` / 目录名里，Electron/Chromium 在 `release/version` 与
#: `LICENSES.chromium.html` 里，FFmpeg 在 DLL 文件名里，Rust core 在二进制里。
#:一处声明，由 `tests/test_licenses.py` 在产物存在时对账（见 `--check` 的注释）。
NON_PYTHON = (
    {"type": "framework", "name": "CPython", "version": "3.13.16",
     "licenses": ["PSF-2.0"], "note": "python/LICENSE.txt"},
    {"type": "library", "name": "Tcl/Tk", "version": "8.6",
     "licenses": ["Tcl/Tk license (BSD-style)"],
     "note": "python/tcl/tk8.6/license.terms"},
    {"type": "framework", "name": "Electron", "version": "44.7.0",
     "licenses": ["MIT"], "note": "release/win-unpacked/LICENSE.electron.txt"},
    {"type": "framework", "name": "Chromium", "version": "bundled with Electron 44.7.0",
     "licenses": ["BSD-3-Clause and third-party notices"],
     "note": "release/win-unpacked/LICENSES.chromium.html"},
    {"type": "library", "name": "FFmpeg (opencv_videoio_ffmpeg500_64.dll)",
     "version": "5.0", "licenses": ["LGPL-2.1"],
     "note": "cv2/LICENSE-3RD-PARTY.txt"},
    {"type": "library", "name": "PDFium", "version": "bundled by pypdfium2 5.14.0",
     "licenses": ["Apache-2.0", "BSD-3-Clause", "CC-BY-4.0"],
     "note": "pypdfium2 dist-info/licenses/LICENSES/"},
    {"type": "library", "name": "resvg (Rust, via resvg_py)", "version": "0.48.1",
     "licenses": ["Apache-2.0 OR MIT"], "note": "resvg_py dist-info/sboms/"},
    {"type": "library", "name": "DocVortex native core (Rust)", "version": "0.5.16",
     "licenses": ["MIT AND Apache-2.0 AND BSD-3-Clause"],
     "note": "docvortex METADATA composite expression"},
    {"type": "library", "name": "React", "version": "19.3.0",
     "licenses": ["MIT"], "note": "bundled into apps/desktop/dist"},
    {"type": "library", "name": "React DOM", "version": "19.3.0",
     "licenses": ["MIT"], "note": "bundled into apps/desktop/dist"},
    {"type": "library", "name": "Mermaid", "version": "12.2.0",
     "licenses": ["MIT"], "note": "bundled into apps/desktop/dist"},
    {"type": "library", "name": "Lucide React", "version": "1.52.0",
     "licenses": ["ISC"], "note": "bundled into apps/desktop/dist"},
    {"type": "library", "name": "DOMPurify", "version": "3.4.17",
     "licenses": ["MPL-2.0 OR Apache-2.0"], "note": "bundled into apps/desktop/dist"},
)


def runtime_site() -> Path:
    """随包运行时的 `site-packages`（SBOM 只认这一个 —— 它才是发出去的东西）。"""
    return (_ROOT / "apps" / "desktop" / "runtime" / "primary-runtime"
            / "python" / "Lib" / "site-packages")


def _read_meta(dist_info: Path) -> dict:
    f = dist_info / "METADATA"
    if not f.is_file():
        return {}
    with f.open("r", encoding="utf-8", errors="replace") as fh:
        msg = Parser().parse(fh)
    classifiers = msg.get_all("Classifier") or []
    lic_classifiers = [c.split(" :: ", 1)[1] for c in classifiers
                       if c.startswith("License ::")]
    return {
        "name": msg.get("Name", dist_info.name.split("-")[0]),
        "version": msg.get("Version", ""),
        "expression": (msg.get("License-Expression") or "").strip(),
        "license": (msg.get("License") or "").strip(),
        "classifiers": lic_classifiers,
    }


def _license_file_hint(dist_info: Path) -> str:
    """元数据没写许可时，读 wheel 自带的 licenses/ 文件（例如 `resvg_py`）。

    只认三种最常见的正文指纹；认不出就不猜（返回空串）。
    """
    lic_dir = dist_info / "licenses"
    files = sorted(lic_dir.glob("*")) if lic_dir.is_dir() else []
    for f in files:
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if "Permission is hereby granted, free of charge" in text:
            return f"MIT (from licenses/{f.name})"
        if "Apache License" in text and "Version 2.0" in text:
            return f"Apache-2.0 (from licenses/{f.name})"
        if "Redistribution and use in source and binary forms" in text:
            return f"BSD (from licenses/{f.name})"
    return ""


def _labels(meta: dict) -> list[str]:
    labels: list[str] = []
    if meta["expression"]:
        labels.append(meta["expression"])
    for c in meta["classifiers"]:
        c = c.replace("OSI Approved :: ", "").strip()
        if c not in labels:
            labels.append(c)
    if not labels and meta["license"]:
        text = " ".join(meta["license"].split())
        labels.append(text[:100])
    return labels


def _families(text: str) -> list[str]:
    if not text.strip():
        return ["UNKNOWN"]
    hits = [name for name, rx in _FAMILIES if rx.search(text)]
    # 「GNU Lesser General Public License」里也含「General Public License」，
    #   「Affero General Public License」同理，有 LGPL/AGPL 时不再单列 GPL。
    #   否则 pystray / PTB 会被同时标成 GPL，看起来像多了两条硬阻塞。
    if "GPL" in hits and ("LGPL" in hits or "AGPL" in hits):
        hits.remove("GPL")
    return hits


def _is_permissive(text: str) -> bool:
    return bool(_PERMISSIVE.search(text))


def _compact(meta: dict, labels: list[str]) -> str:
    """给 SBOM / 表格用的短标签：表达式优先，其次 classifier，最后原文截断。"""
    if meta["expression"]:
        return meta["expression"]
    cls = [c.replace("OSI Approved :: ", "").strip() for c in meta["classifiers"]]
    if cls:
        return " / ".join(dict.fromkeys(cls))
    if meta["license"]:
        return " ".join(meta["license"].split())[:100]
    return labels[0] if labels else "UNKNOWN"


#: 随包运行时里"有代码、没有 dist-info"的那些 —— 它们来自 sdist。
#:
#: `prepare_runtime.py` 的 sdist 路径按设计只把声明的包目录搬进 site-packages
#: （不跑 pip、不 exec），所以不会产生 `.dist-info`；而 `collect()` 按 dist-info
#: 枚举，天生看不见它们。少一条的后果是"依赖在用、声明里没有"，而 `--check`
#: 会一直是绿的 —— 挡住它的是 `tests/test_licenses.py` 里「锁 ⊆ 披露」那条守卫。
SDIST_INSTALLED = (
    {"name": "jieba", "version": "0.42.1", "compact": "MIT", "families": []},
)


def collect(site: Path) -> list[dict]:
    rows = []
    for di in sorted(site.glob("*.dist-info")):
        meta = _read_meta(di)
        if not meta:
            continue
        labels = _labels(meta)
        if not labels:
            hint = _license_file_hint(di)
            if hint:
                labels = [hint]
        joined = " | ".join(labels)
        fams = _families(joined)
        rows.append({
            "name": meta["name"],
            "version": meta["version"],
            "labels": labels,
            "joined": joined,
            "compact": _compact(meta, labels),
            "families": fams,
            "permissive": _is_permissive(joined),
        })
    # 有代码、没 dist-info 的那几条（见 `SDIST_INSTALLED`）—— 手工补进来。
    for x in SDIST_INSTALLED:
        rows.append({
            "name": x["name"], "version": x["version"], "labels": list(x["compact"] and [x["compact"]] or []),
            "joined": x["compact"], "compact": x["compact"],
            "families": list(x["families"]), "permissive": not x["families"],
        })
    return rows


def _purl(name: str, version: str) -> str:
    return "pkg:pypi/%s@%s" % (name.lower().replace("_", "-"), version)


# ---------------------------------------------------------------------------
# 前端：Vite 打进 `apps/desktop/dist` 的那批（含传递依赖）
# ---------------------------------------------------------------------------
#
# SBOM 里前端只列直接依赖不够：真正打进产物的还有一百来个传递依赖 ——
#    少列 = 发出去的字节里有东西没署。
#从 `package.json` 的直接依赖出发，沿 `package-lock.json` 的 `dependencies` 走闭包。
#
# 这是上界（Vite 会 tree-shake 掉一部分）。SBOM 宁可多列不少列：
#    多列一个的代价是清单长一行，少列一个的代价是署名义务没履行。

_LOCKFILE = _ROOT / "package-lock.json"
_FRONTEND_MANIFEST = _ROOT / "package.json"
_NODE_MODULES = _ROOT / "node_modules"

#: 许可是文件级 copyleft 的那几家 —— 前端闭包里出现要给它们一个显式交代。
_FRONTEND_WATCH = ("EPL", "MPL", "GPL", "LGPL", "CC-BY-SA")


def _lock_entry(pk: dict, name: str, parent: str = "") -> str | None:
    """Node 的解析顺序：先父级嵌套，再根；都没有，None（它没被装上）。"""
    if parent:
        nested = parent + "/node_modules/" + name
        if nested in pk:
            return nested
    root = "node_modules/" + name
    return root if root in pk else None


def frontend_rows() -> list[dict]:
    """前端构建产物里可能出现的 npm 包（直接依赖 + 传递闭包）。

    返回项：`{name, version, license, texts: [(文件名, 正文), …]}`。
    不联网、不猜：版本与许可取自 `package-lock.json`，
    许可正文取自磁盘上的 `node_modules/<包>/LICEN[CS]E*`。
    """
    if not (_LOCKFILE.is_file() and _FRONTEND_MANIFEST.is_file()):
        return []
    lock = json.loads(_LOCKFILE.read_text(encoding="utf-8"))
    pk = lock.get("packages") or {}
    manifest = json.loads(_FRONTEND_MANIFEST.read_text(encoding="utf-8"))
    roots = sorted(manifest.get("dependencies") or {})

    seen: dict[str, None] = {}
    queue = [k for k in (_lock_entry(pk, r) for r in roots) if k]
    while queue:
        key = queue.pop()
        if key in seen or key not in pk:
            continue
        seen[key] = None
        for dep in (pk[key].get("dependencies") or {}):
            nxt = _lock_entry(pk, dep, key)
            if nxt:
                queue.append(nxt)

    out = []
    for key in sorted(seen):
        entry = pk[key]
        # 本地 workspace 链接（`@crannia/ui`）是我们自己的代码，不是第三方。
        if entry.get("link") or not entry.get("version"):
            continue
        name = key.rsplit("node_modules/", 1)[-1]
        texts = []
        pkg_dir = _ROOT / key
        if pkg_dir.is_dir():
            for p in sorted(pkg_dir.iterdir()):
                if not p.is_file():
                    continue
                if re.match(r"(?i)^(licen[cs]e|copying|copyright)", p.name):
                    try:
                        texts.append((p.name,
                                      p.read_text(encoding="utf-8", errors="replace")))
                    except OSError:
                        pass
        out.append({
            "name": name,
            "version": entry["version"],
            "license": entry.get("license") or "UNKNOWN（见许可原文）",
            "texts": texts,
        })
    return out


def render_frontend_markdown(rows: list[dict]) -> str:
    """`THIRD-PARTY-FRONTEND.md`：清单 + 逐包许可原文。

    判据：MIT / ISC / BSD 一族的义务是"把版权声明与许可文本一起发出去"。
    只列名字与 SPDX 标识不算履约 —— 所以这里把原文一并落盘，随安装包走。
    """
    watch = [r for r in rows
             if any(w in str(r["license"]).upper() for w in _FRONTEND_WATCH)]
    lines = [
        "# 前端构建产物里的第三方包（自动生成）",
        "",
        "> **生成命令**：`.venv/Scripts/python.exe scripts/audit_licenses.py --all`",
        "> **数据源**：根 `package.json` 的直接依赖 + `package-lock.json` 的传递闭包；",
        "> 许可正文取自 `node_modules/<包>/LICEN[CS]E*`。",
        "",
        "这些包被 Vite 打进 `apps/desktop/dist/assets`，运行时不再读 `node_modules` ——",
        "署名义务靠这份文件履行。（`scripts/audit_licenses.py` 取的是闭包：",
        "Vite 会 tree-shake 掉一部分，清单是上界。）",
        "",
        f"共 **{len(rows)}** 个包。",
    ]
    if watch:
        lines += [
            "",
            "> **[!] 其中这几家的许可不是纯宽松的**（文件级 copyleft / 弱 copyleft），",
            "> 分发时要额外确认：**不改它们的源码**、**许可原文随包**（本文件已附）。",
            "",
            "| 包 | 版本 | 许可 |",
            "|---|---|---|",
        ]
        for r in watch:
            lines.append("| `%s` | %s | %s |" % (r["name"], r["version"],
                                                 str(r["license"]).replace("|", "/")))
    lines += [
        "",
        "| # | 包 | 版本 | 许可 |",
        "|---:|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        lines.append("| %d | `%s` | %s | %s |"
                     % (i, r["name"], r["version"],
                        str(r["license"]).replace("|", "/")))
    lines += [
        "",
        "---",
        "",
        "## 许可原文（逐包）",
        "",
        "> 下面是**原样**摘出来的许可文件。多个文件时按文件名顺序拼接。",
        "",
    ]
    for r in rows:
        lines += ["### `%s` %s —— %s" % (r["name"], r["version"], r["license"]), ""]
        if not r["texts"]:
            lines += ["（`node_modules` 里没有找到许可文件 —— 这一条要人工补。）", ""]
            continue
        for fname, text in r["texts"]:
            lines += ["`%s`" % fname, "", "~~~~", text.strip("\n"), "~~~~", ""]
    lines.append("")
    return "\n".join(lines)


def build_sbom(rows: list[dict], frontend: list[dict] | None = None) -> dict:
    """CycloneDX 1.5 的最小子集（确定、可 diff、可对账）。

    注意：不写时间戳：`--check` 要与仓里的那一份逐字节比，时间戳会让它永远不相等。
    """
    comps = []
    for r in rows:
        comp: dict = {
            "type": "library",
            "name": r["name"],
            "version": r["version"],
            "purl": _purl(r["name"], r["version"]),
            "licenses": [{"license": {"name": r["compact"]}}],
        }
        if r["families"]:
            comp["properties"] = [
                {"name": "crannia:license-family", "value": ",".join(r["families"])}
            ]
        comps.append(comp)
    for c in NON_PYTHON:
        comps.append({
            "type": c["type"],
            "name": c["name"],
            "version": c["version"],
            "licenses": [{"license": {"name": x}} for x in c["licenses"]],
            "properties": [{"name": "crannia:license-file", "value": c["note"]}],
        })
    for r in (frontend or []):
        comps.append({
            "type": "library",
            "name": r["name"],
            "version": r["version"],
            "purl": "pkg:npm/%s@%s" % (r["name"].replace("@", "%40"), r["version"]),
            "licenses": [{"license": {"name": str(r["license"])}}],
            "properties": [{"name": "crannia:bundled",
                            "value": "frontend (apps/desktop/dist, Vite)"}],
        })
    comps.sort(key=lambda c: c["name"].lower())
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": "Crannia"},
        },
        "components": comps,
    }


def render_markdown(rows: list[dict]) -> str:
    lines = [
        "# 随包 Python 依赖许可清单（自动生成）",
        "",
        "> **生成命令**：`.venv/Scripts/python.exe scripts/audit_licenses.py --md`",
        "> **数据源**：`apps/desktop/runtime/primary-runtime/python/Lib/site-packages`",
        "> 的 `*.dist-info/METADATA`。",
        "",
        f"共 **{len(rows)}** 个发行包。`[!]` = 需要履约的 copyleft 家族",
        "（LGPL / MPL，见 `THIRD-PARTY.md` §8 / §9）。",
        "",
        "| # | 包 | 版本 | 许可 |",
        "|---:|---|---|---|",
    ]
    for i, r in enumerate(rows, 1):
        mark = "[!] " if r["families"] else ""
        lic = r["compact"].replace("|", "/")
        lines.append(f"| {i} | `{r['name']}` | {r['version']} | {mark}{lic} |")
    lines += [
        "",
        "---",
        "",
        "非 Python 组件（CPython / Tcl-Tk / Electron / Chromium / FFmpeg / PDFium /",
        "Rust core / 前端 5 件）见 `sbom.json` 的 `crannia:license-file` 属性与",
        "`THIRD-PARTY.md` §10。",
        "",
    ]
    return "\n".join(lines)


def default_site() -> Path:
    for p in (_ROOT / ".venv" / "Lib" / "site-packages",
              _ROOT / ".venv" / "lib" / "python3.13" / "site-packages"):
        if p.is_dir():
            return p
    raise SystemExit("找不到 .venv 的 site-packages，请显式给目录")


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    flag = "--flag" in argv
    as_json = "--json" in argv
    gen = any(x in argv for x in ("--sbom", "--md", "--all", "--check"))
    if args:
        site = Path(args[0])
    elif gen and runtime_site().is_dir():
        site = runtime_site()
    else:
        site = default_site()
    if not site.is_dir():
        raise SystemExit(f"不是目录：{site}")

    rows = collect(site)

    if gen:
        fe_rows = frontend_rows()
        sbom_text = json.dumps(build_sbom(rows, fe_rows), ensure_ascii=False,
                               indent=2) + "\n"
        md_text = render_markdown(rows)
        fe_text = render_frontend_markdown(fe_rows)
        sbom_path = _ROOT / "sbom.json"
        md_path = _ROOT / "THIRD-PARTY-PYTHON.md"
        fe_path = _ROOT / "THIRD-PARTY-FRONTEND.md"
        if "--check" in argv:
            bad = 0
            for p, text in ((sbom_path, sbom_text), (md_path, md_text),
                            (fe_path, fe_text)):
                old = p.read_text(encoding="utf-8") if p.is_file() else ""
                if old != text:
                    bad += 1
                    print(f"[check] 不一致：{p.name}（重跑不带 --check 的）")
            if bad:
                return 1
            print(f"[check] 一致：{sbom_path.name} · {md_path.name} · {fe_path.name}"
                  f"（{len(rows)} 个 Python 包 + {len(fe_rows)} 个前端包 + "
                  f"{len(NON_PYTHON)} 个非 Python 组件）")
            return 0
        if "--sbom" in argv or "--all" in argv:
            sbom_path.write_text(sbom_text, encoding="utf-8", newline="\n")
            print(f"[sbom] 写好了：{sbom_path}")
        if "--md" in argv or "--all" in argv:
            md_path.write_text(md_text, encoding="utf-8", newline="\n")
            print(f"[sbom] 写好了：{md_path}")
            fe_path.write_text(fe_text, encoding="utf-8", newline="\n")
            print(f"[sbom] 写好了：{fe_path}")
        if "--all" in argv:
            print(f"[sbom] {len(rows)} 个 Python 包 + {len(fe_rows)} 个前端包 + "
                  f"{len(NON_PYTHON)} 个非 Python 组件")
        return 0
    if as_json:
        print(json.dumps([{k: r[k] for k in ("name", "version", "labels", "families")}
                          for r in rows], ensure_ascii=False, indent=2))
        return 0

    total = len(rows)
    flagged = [r for r in rows if r["families"] != [] and "UNKNOWN" not in r["families"]]
    unknown = [r for r in rows if r["families"] == ["UNKNOWN"]]

    print(f"[scan] {site}")
    print(f"[scan] {total} 个发行包")

    if flag:
        for r in flagged:
            print(f"  {r['name']:<28} {r['version']:<12} "
                  f"{','.join(r['families']):<12} {r['joined'][:110]}")
    else:
        groups: dict[str, list[dict]] = {}
        for r in rows:
            key = r["families"][0] if r["families"] else "PERMISSIVE"
            if not r["families"] and not r["permissive"]:
                key = "UNKNOWN"
            groups.setdefault(key, []).append(r)
        for key in sorted(groups):
            names = groups[key]
            print(f"\n== {key} ({len(names)}) ==")
            for r in names:
                print(f"  {r['name']:<28} {r['version']:<12} {r['joined'][:110]}")

    print(f"\n[flag] 需要人看一眼：{len(flagged)}")
    for r in flagged:
        print(f"  - {r['name']} {r['version']}: {r['joined'][:120]}")
    print(f"[unknown] 元数据里查不到许可：{len(unknown)}")
    for r in unknown:
        print(f"  - {r['name']} {r['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
