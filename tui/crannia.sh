#!/usr/bin/env bash
# crannia-tui launcher: type `crannia` anywhere to start.
#
# 与 `crannia.cmd` 同一套布局判据，差别只有两处：路径形状，以及没有
# "换到 Windows Terminal 重开"那一段 —— Linux 的终端都解释 ANSI。
#
# 四种布局共用这一份：
#   A0) 装在 PATH 里的软链：<install>/bin/crannia -> ../resources/app/tui
#   A)  GUI 安装包内：本文件旁边有 resources/
#   B)  开发树：本文件在 <repo>/tui/ 下，解释器是同目录 .venv/bin/python
#   C)  独立包：<pkg>/crannia.sh + <pkg>/runtime/python/bin/python3，
#       代码在 <pkg>/releases/<current>/crannia_tui
#
# 退出码必须原样带出去：doctor / --version / --once 是给脚本判的。
#
# 这份文件必须是 LF 换行。CRLF 会让 bash 把行尾的 \r 当成命令的一部分，
# 报错看起来像"找不到命令"。

set -u

# 软链要解到真实文件。装到 ~/.local/bin 时这里通常是软链，不解的话下面
# 每一条"同目录下有没有 resources/"都在 PATH 目录里找，永远找不到。
self=$0
if resolved=$(readlink -f -- "$self" 2>/dev/null) && [ -n "$resolved" ]; then
    self=$resolved
fi
HERE=$(cd -- "$(dirname -- "$self")" && pwd -P) || exit 1

# 调用时所在的目录就是会话的工作区，cd 到代码根之前先记下来。
CRANNIA_LAUNCH_CWD=$(pwd -P)
export CRANNIA_LAUNCH_CWD

# 更新链（releases/ 与 current）落在本文件旁边，不是下面要 cd 进去的代码根。
export CRANNIA_LAUNCH_ROOT=$HERE

PY=
RUN=

# A0) PATH 里的软链：<install>/bin/crannia -> ../resources/...
if [ -d "$HERE/../resources/app/tui/crannia_tui" ]; then
    PY="$HERE/../resources/primary-runtime/python/bin/python3"
    RUN="$HERE/../resources/app/tui"
fi

# A) GUI 安装包内
if [ -z "$PY" ] && [ -d "$HERE/resources/app/tui/crannia_tui" ]; then
    PY="$HERE/resources/primary-runtime/python/bin/python3"
    RUN="$HERE/resources/app/tui"
fi

# B) 开发树
if [ -z "$PY" ] && [ -x "$HERE/.venv/bin/python" ]; then
    PY="$HERE/.venv/bin/python"
fi

# C) 独立包。它也是安装版，但代码根是 releases/<version>，不是
# <install>/resources/app —— 路径判据认不出来，得明说；否则用户数据会
# 落到 releases/<version>/data 里，升级一次就跟着没了。
if [ -z "$PY" ] && [ -x "$HERE/runtime/python/bin/python3" ]; then
    PY="$HERE/runtime/python/bin/python3"
    export CRANNIA_INSTALLED=1
fi

if [ -z "$PY" ]; then
    echo "[crannia] no Python runtime found next to $self" >&2
    echo "[crannia] expected .venv/bin/python or runtime/python/bin/python3" >&2
    exit 1
fi

# 版本指针：current 里写一个目录名。有就运行那一份，没有就运行本树。
[ -n "$RUN" ] || RUN=$HERE
if [ -f "$HERE/current" ]; then
    CUR=$(head -n 1 "$HERE/current" | tr -d '[:space:]')
    if [ -n "$CUR" ] && [ -d "$HERE/releases/$CUR/crannia_tui" ]; then
        RUN="$HERE/releases/$CUR"
    fi
fi

# `-m` 要求包从当前目录可导入。不 cd 的话，在任何别的目录里敲 crannia
# 都是 "No module named crannia_tui"。
cd -- "$RUN" || exit 1
exec "$PY" -m crannia_tui "$@"
