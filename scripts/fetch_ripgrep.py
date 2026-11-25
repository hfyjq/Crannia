# -*- coding: utf-8 -*-
"""取一份随包的 `ripgrep` 二进制（开发树用；安装版由打包带进来）。

    python scripts/fetch_ripgrep.py
    python scripts/fetch_ripgrep.py --check     # 只校验，不下载

## 它做什么

1. 按当前平台挑一份钉死的来源（`_SOURCES`）；
2. 从 npm registry 拉那个 tgz；
3. 解出里面的 `bin/rg`，核 sha256（对不上就停 —— 不写任何东西）；
4. 写到 `vendor/ripgrep/<平台目录>/rg`，非 Windows 补可执行位。

运行期绝不下载：用户装一次就该齐活。这个脚本只在开发树缺文件时跑。
`vendor/ripgrep/` 的文件入库（两个平台合计约 11 MB），正常 clone 下来就有；
脚本主要给"换版本 / 补另一台机器"用。

平台目录名不在这里另抄一份 —— 它和 `core/rg.py::_PLATFORM_DIRS` 是同一份，
两边各写一套的话，加平台时迟早有一边忘了改。
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.rg import _PLATFORM_DIRS  # noqa: E402

#: 每个平台钉死的来源。改版本时四样一起改（tarball / member / sha256 / 大小），
#: 并同步 `vendor/ripgrep/README.md` 与 `THIRD-PARTY.md`。
_SOURCES = {
    "win32-x64": {
        "package": "@vscode/ripgrep-win32-x64",
        "version": "1.18.0",
        "tarball": ("https://registry.npmjs.org/@vscode/ripgrep-win32-x64/-/"
                    "ripgrep-win32-x64-1.18.0.tgz"),
        "member": "package/bin/rg.exe",
        "sha256": "f9dde63498b3193f098355dbec97af99dc4f6b8fa0df5ed04114a03012c042cb",
        "name": "rg.exe",
    },
    "linux-x64": {
        "package": "@vscode/ripgrep-linux-x64",
        "version": "1.18.0",
        "tarball": ("https://registry.npmjs.org/@vscode/ripgrep-linux-x64/-/"
                    "ripgrep-linux-x64-1.18.0.tgz"),
        "member": "package/bin/rg",
        "sha256": "193906679498de4d939345b937fa24e0e69a03c244bd70c859f5e41232713f21",
        "name": "rg",
    },
}


def _platform() -> str:
    """这台机器该取哪一份。判据与 `core.rg.platform_dir()` 同一来源。"""
    try:
        import platform as _pf
        machine = str(_pf.machine() or "").lower()
    except Exception:                                        # noqa: BLE001
        machine = ""
    name = _PLATFORM_DIRS.get((sys.platform, machine))
    if not name or name not in _SOURCES:
        raise SystemExit(
            "[rg] 这个平台（%s/%s）没有钉死的来源。\n"
            "  已有：%s\n"
            "  要加新平台：从 npm 取一份 `@vscode/ripgrep-<平台>`，"
            "把里面 member 的 sha256 填进 `_SOURCES`。"
            % (sys.platform, machine or "?", "、".join(sorted(_SOURCES))))
    return name


def _dest(key: str) -> Path:
    return ROOT / "vendor" / "ripgrep" / key / _SOURCES[key]["name"]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_existing(key: str) -> bool:
    dest = _dest(key)
    want = _SOURCES[key]["sha256"]
    if not dest.is_file():
        print("[rg] 还没有 %s" % dest.relative_to(ROOT))
        return False
    got = _sha256(dest.read_bytes())
    if got != want:
        print("[rg] [!] 现有文件的 sha256 对不上：\n  实得 %s\n  期望 %s" % (got, want))
        return False
    print("[rg] [ok] 现有文件校验通过（%s，%d 字节）"
          % (dest.relative_to(ROOT), dest.stat().st_size))
    return True


def _fetch(key: str) -> int:
    src = _SOURCES[key]
    print("[rg] 下载 %s@%s ..." % (src["package"], src["version"]))
    try:
        with urllib.request.urlopen(src["tarball"], timeout=120) as resp:
            blob = resp.read()
    except Exception as exc:                                 # noqa: BLE001
        print("[rg] 下载失败（%s: %s）\n"
              "   → 这台机器多半连不上 npm registry；"
              "换一台能上网的机器跑一次，把 `vendor/ripgrep/` 整个拷过来即可。"
              % (type(exc).__name__, exc))
        return 1
    print("[rg] 包 %d 字节" % len(blob))
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            member = tar.extractfile(src["member"])
            if member is None:
                print("[rg] 包里没有 %s —— 上游结构变了？" % src["member"])
                return 1
            data = member.read()
    except Exception as exc:                                 # noqa: BLE001
        print("[rg] 解包失败（%s: %s）" % (type(exc).__name__, exc))
        return 1
    got = _sha256(data)
    if got != src["sha256"]:
        print("[rg] ★ sha256 对不上，**什么都没写**：\n"
              "  实得 %s\n  期望 %s\n"
              "  → 上游换了内容？核对 `vendor/ripgrep/README.md` 再改这里。"
              % (got, src["sha256"]))
        return 1
    dest = _dest(key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    #: tgz 里的权限位不会落到文件系统上。Linux 少了执行位，rg 起不来的报错是
    #: `Permission denied` —— 看着像文件坏了，其实只是没 chmod。
    if src["name"] != "rg.exe":
        dest.chmod(0o755)
    print("[rg] [ok] 写入 %s（%d 字节，sha256 校验通过）"
          % (dest.relative_to(ROOT), len(data)))
    #: 来源留档（换版本时人一眼看得见每一份是怎么来的）
    src_doc = dest.parent.parent / "package-source.json"
    try:
        src_doc.write_text(
            json.dumps(_SOURCES, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    except OSError:
        pass
    return 0


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    key = _platform()
    if "--check" in args:
        return 0 if _check_existing(key) else 1
    if _check_existing(key):
        return 0
    return _fetch(key)


if __name__ == "__main__":
    sys.exit(main())
