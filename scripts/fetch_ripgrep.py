# -*- coding: utf-8 -*-
"""取一份随包的 `ripgrep` 二进制（开发树用；安装版由打包带进来）。

    .venv/Scripts/python.exe scripts/fetch_ripgrep.py
    .venv/Scripts/python.exe scripts/fetch_ripgrep.py --check     # 只校验，不下载

## 它做什么

1. 从 npm registry 拉钉死版本的那个包；
2. 核 sha256（对不上就停 —— 不写任何东西）；
3. 解出 `package/bin/rg.exe` 放到 `vendor/ripgrep/win32-x64/rg.exe`。

运行期绝不下载：用户装一次就该齐活。
   这个脚本只在开发树缺文件时跑。
`vendor/ripgrep/` 的文件入库（5.4 MB），所以正常 clone 下来就有；
   这个脚本主要给"换版本 / 补另一台机器"用。
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

#: 钉死的东西（改版本时三样一起改，并把 `vendor/ripgrep/README.md` 同步）。
PACKAGE = "@vscode/ripgrep-win32-x64"
VERSION = "1.18.0"
TARBALL = ("https://registry.npmjs.org/@vscode/ripgrep-win32-x64/-/"
           "ripgrep-win32-x64-1.18.0.tgz")
SHA256 = "f9dde63498b3193f098355dbec97af99dc4f6b8fa0df5ed04114a03012c042cb"
MEMBER = "package/bin/rg.exe"
DEST = ROOT / "vendor" / "ripgrep" / "win32-x64" / "rg.exe"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_existing() -> bool:
    if not DEST.is_file():
        print("[rg] 还没有 %s" % DEST.relative_to(ROOT))
        return False
    got = _sha256(DEST.read_bytes())
    if got != SHA256:
        print("[rg] [!] 现有文件的 sha256 对不上：\n  实得 %s\n  期望 %s" % (got, SHA256))
        return False
    print("[rg] [ok] 现有文件校验通过（%s，%d 字节）"
          % (DEST.relative_to(ROOT), DEST.stat().st_size))
    return True


def _fetch() -> int:
    print("[rg] 下载 %s@%s ..." % (PACKAGE, VERSION))
    try:
        with urllib.request.urlopen(TARBALL, timeout=120) as resp:
            blob = resp.read()
    except Exception as exc:                                # noqa: BLE001
        print("[rg] 下载失败（%s: %s）\n"
              "   → 这台机器多半连不上 npm registry；"
              "换一台能上网的机器跑一次，把 `vendor/ripgrep/` 整个拷过来即可。"
              % (type(exc).__name__, exc))
        return 1
    print("[rg] 包 %d 字节" % len(blob))
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            member = tar.extractfile(MEMBER)
            if member is None:
                print("[rg] 包里没有 %s —— 上游结构变了？" % MEMBER)
                return 1
            data = member.read()
    except Exception as exc:                                # noqa: BLE001
        print("[rg] 解包失败（%s: %s）" % (type(exc).__name__, exc))
        return 1
    got = _sha256(data)
    if got != SHA256:
        print("[rg] ★ sha256 对不上，**什么都没写**：\n"
              "  实得 %s\n  期望 %s\n"
              "  → 上游换了内容？核对 `vendor/ripgrep/README.md` 再改这里。"
              % (got, SHA256))
        return 1
    DEST.parent.mkdir(parents=True, exist_ok=True)
    DEST.write_bytes(data)
    print("[rg] [ok] 写入 %s（%d 字节，sha256 校验通过）"
          % (DEST.relative_to(ROOT), len(data)))
    #: 来源留档（换版本时人一眼看得见这一份是怎么来的）
    src = DEST.parent.parent / "package-source.json"
    try:
        src.write_text(json.dumps(
            {"name": PACKAGE, "version": VERSION, "tarball": TARBALL,
             "member": MEMBER, "sha256": SHA256},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return 0


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--check" in args:
        return 0 if _check_existing() else 1
    if _check_existing():
        return 0
    return _fetch()


if __name__ == "__main__":
    sys.exit(main())
