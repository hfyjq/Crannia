# -*- coding: utf-8 -*-
"""tiktoken 的唯一入口 —— 外加「安装版第一次启动别去联网」。

## 为什么要单独一个文件

两条判据，各自都值得一个入口：

① 词表必须随包走。 `tiktoken.get_encoding("cl100k_base")` 不是纯离线：
   它第一次调用会去 `openaipublic.blob.core.windows.net` 下载 ~1.6 MB 的 BPE
   文件（缓存到 `%TEMP%\\data-gym-cache`）。装出来的应用在**没有外网、或那个域名
   被墙**的机器上，会在 provider 构造那一刻抛 —— 表现是"模型配好了，
   却一句话都发不出去"，而报错是一串 `HTTPSConnectionPool(...)`，
   看不出"我们本来应该自带这个词表"。

`scripts/prepare_runtime.py::vendor_tiktoken_cache` 把那份文件放进
      `<runtime>/tiktoken-cache/`（文件名的算法见 tiktoken `load.py::read_file_cached`：
      sha1(url)），这里在第一次调用前把 `TIKTOKEN_CACHE_DIR` 指过去。

   清空缓存 + 断网，`ProxyError`，provider 直接构造不出来。

② 失败要说得清。缓存不在、网也不通时，原生异常里没有任何线索说明
   "这是随包该带的东西"。

## 一条不许动的

只设默认值（`setdefault`）—— 用户/测试自己设过 `TIKTOKEN_CACHE_DIR` 就听它的
（构建期的负对照正是靠把它指到一个空目录、再把网掐掉，来验"包里的缓存真的被用上了"）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

#: 随包缓存目录的名字（`scripts/prepare_runtime.py` 里那一侧同一个常量）。
CACHE_DIRNAME = "tiktoken-cache"


def bundled_cache_dir() -> Path | None:
    """随包那份词表缓存目录；不在（开发环境的 `.venv`）就回 `None`。

    位置是 `<runtime>/tiktoken-cache`，而 `sys.prefix` 指向 `<runtime>/python`
取它的上一级。开发环境下 `.venv` 旁边没有这个目录，回 `None`，
    行为与从前逐字相同（照旧缓存到 `%TEMP%`）。
    """
    try:
        base = Path(sys.prefix).resolve().parent / CACHE_DIRNAME
    except Exception:                                            # noqa: BLE001
        return None
    return base if base.is_dir() else None


def get_encoding(name: str = "cl100k_base") -> Any:
    """取一个 tiktoken 编码器 —— 全仓唯一入口（别处不许再直调 `tiktoken`）。

    失败时抛 `RuntimeError`（不是原生的网络异常）：这条路上唯一的出路
    就是"把词表带上"，所以错误文本必须说出这一点，否则下一个人只会去查网络。
    """
    import tiktoken

    cache = bundled_cache_dir()
    if cache is not None:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(cache))
    try:
        return tiktoken.get_encoding(name)
    except Exception as exc:                                     # noqa: BLE001
        where = (str(cache) if cache is not None
                 else "<runtime>/%s/（开发环境没有它，联网一次即可）" % CACHE_DIRNAME)
        raise RuntimeError(
            "取不到 tiktoken 词表 %r（%s: %s）。\n"
            "  → 随包应当自带 %s（文件名 = sha1(词表 URL)）。\n"
            "  → 若这是开发环境，联网一次即可缓存到 %%TEMP%%；\n"
            "  → 若这是安装版，说明 `scripts/prepare_runtime.py` 那一步没把词表带进去。"
            % (name, type(exc).__name__, str(exc)[:300], where)
        ) from exc
