"""python -m web — start the Crannia Web UI or desktop backend."""
import os
import sys
from pathlib import Path

# 数据根必须在导入 web.server 之前就位：安装版要迁移并 chdir，
#   而后面所有 `data/...` 都是相对路径。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import paths

_rep = paths.prepare()

# 数据格式比本程序新时，`prepare()` 已经备份过整个数据根
#    （「备份 + 继续 + 大声说」）—— 这一行就是「大声说」。
#    备份不完整不许写成"已备份"（标签在说谎）。
_ds = (_rep or {}).get("data_schema") or {}
if _ds.get("compat"):
    if _ds.get("complete") and _ds.get("backup"):
        print("[paths] 数据格式 %s 高于本地 %s —— 已备份到 %s，兼容模式继续"
              % (_ds.get("found"), _ds.get("ours"), _ds.get("backup")))
    else:
        print("[paths] !! 数据格式 %s 高于本地 %s，但备份没做成（%s）"
              " —— 请先手动复制整个数据目录再使用"
              % (_ds.get("found"), _ds.get("ours"),
                 _ds.get("failed") or _ds.get("error") or "未知原因"))

_mig_errors = (_rep or {}).get("errors") or []
if _mig_errors:
    print("[paths] !! 数据迁移没有完成（%s）—— 旧数据仍在安装目录，"
          "排除原因后重启会继续" % "; ".join(_mig_errors))

# 上面这两行只是终端里的读数。用户看得到的横幅（`#02`）与模型手里的
#   `build_note()` 读的是 `data/incidents` 那份账 —— 记账发生在
#   `paths.prepare()` 末尾（一处口径，三个入口都覆盖）。


from web.server import start_webui

# 界面语言在启动时说一行：默认已经是「跟随系统」，
#    而"系统是哪个、我们认成了哪个"此前没有任何地方说过 —— 用户看到英文界面
#    却找不到是谁决定的。这一行就是那个读数。
#    壳传的是 `CRANNIA_SYSTEM_LOCALE`（它读得到 `app.getLocale()`）；
#      源码跑则由 `core/i18n.py` 自己去探平台。
try:
    from core import i18n as _i18n
    #: 壳传下来的原始标签（空 = 没有壳，i18n 会自己去探平台）。
    #: 这一行是真的读，不是注释里提一嘴 —— `test_brand_identifiers` 那条
    #:   「壳设的变量 Python 得用同一个拼写读」查的就是它。
    _tag = (os.environ.get("CRANNIA_SYSTEM_LOCALE") or "").strip()
    _cfg = _i18n.configured()
    print("[i18n] 界面语言: %s（%s；系统: %s -> %s）"
          % (_i18n.current(),
             ("跟随系统" if _cfg is None else "用户选了 %s" % _cfg),
             (_tag or "本机探测"), _i18n.system_language()))
except Exception as _exc:                                    # noqa: BLE001
    print("[i18n] 语言状态读不出来（%s: %s）—— 照常启动"
          % (type(_exc).__name__, _exc))

start_webui(
    port=int(os.environ.get("CRANNIA_API_PORT", "5000")),
    open_browser=os.environ.get("CRANNIA_DESKTOP") != "1",
)
