"""TUI 读的那一列设置键，以及启动时的播种 / 坏文件处置。

三条口径：

  1. 一份文件，各读各的。`data/settings.json` 与 GUI 共用；TUI 只读下面
     白名单这一列，其余键存在但不读，砍掉的功能不靠删键，靠不读。
  2. 首次启动播种。缺文件就用出厂基准写一份（`core/settings_baseline.py`
     的基准文件）；坏 JSON 就先备份再按默认重建。所以这里走一次子进程，
     TUI 不 import `core`（两套安装各自更新）。
  3. 坏值回默认并出声。读的时候类型不对就退回基准值、把那一格记进
     `problems`，由启动路径打一行；不写回文件（用户的文件由用户改）。

模型那一格不在设置里 —— 它住在端配置（`data/runtime/model_configs.json`），
TUI 从 `/api/status` 读到的是生效后的模型名。
"""

from __future__ import annotations

import json
from pathlib import Path

from .doctor import make_data_root_query, query
from .i18n import t

#: TUI 支持的键。`type` 或一组可接受的类型；`int` 键**不许是 bool**
#: （`isinstance(True, int)` 是 True，不单独判会把 `true` 读成 1）。
SUPPORTED_KEYS: dict[str, tuple] = {
    "effort_level": (int,),
    "default_mode": (str,),
    "approval_timeout_seconds": (int,),
    "auto_approve": (bool,),
    "cross_zone_enabled": (bool,),
    "relax_command_tier": (bool,),
    "no_ask_readonly": (bool,),
    "full_access_unlock": (bool,),
    "interface_language": (str, type(None)),
    "response_language": (str, type(None)),
    "trace_full": (bool,),
    "workspace": (str,),
    #: 通道与定时任务是否随这个后端跑。TUI 首启播种为关。
    "background_services": (bool,),
}

#: 首次播种时改写的几格 —— 只影响"这一次新建出来的文件"，
#: 已有文件里是用户的决定（`heal()` 那一支直接返回 `ok`，不会走到这里）。
_SEED_OVERRIDES: dict = {"background_services": False}

_SETTINGS_REL = Path("data") / "settings.json"
_BASELINE_REL = Path("data") / "settings.example.json"

#: 播种 / 修复要问核心的那一句。返回 `heal()` 的报告（JSON）。
_HEAL_CODE = (
    "import json; from pathlib import Path; "
    "from core.settings_baseline import heal; "
    "print(json.dumps(heal(Path(%r), overrides=json.loads(%r)), "
    "ensure_ascii=False))")


def load_baseline(home: Path) -> dict:
    """读出厂基准（去掉 `_comment` 一类下划线键）。读不到返回空 dict。"""
    try:
        raw = json.loads((Path(home) / _BASELINE_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if not k.startswith("_")} \
        if isinstance(raw, dict) else {}


def _type_ok(key: str, value) -> bool:
    kinds = SUPPORTED_KEYS[key]
    if isinstance(value, bool) and bool not in kinds:
        return False
    return isinstance(value, kinds)


def read(path: Path, baseline: dict) -> tuple[dict, list[str]]:
    """只读白名单那一列。返回 `(值, 问题行)`。

    文件不存在 / 读不出来就返回 `({}, [])`：文件层面的处置归 `ensure()`，
    这里重复报一次只会让同一条坏文件说两遍。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, []
    if not isinstance(data, dict):
        return {}, []
    out: dict = {}
    problems: list[str] = []
    for key in SUPPORTED_KEYS:
        if key not in data:
            continue
        value = data[key]
        if _type_ok(key, value):
            out[key] = value
            continue
        want = "/".join(t.__name__ for t in SUPPORTED_KEYS[key])
        problems.append(t("%s=%r 不是 %s，按默认 %r")
                        % (key, value, want, baseline.get(key)))
        out[key] = baseline.get(key)
    return out, problems


def ensure(python: Path, home: Path, *, query_fn=None,
           root_fn=None) -> list[str]:
    """启动时确保设置文件可用；返回给人看的几行（不抛、不挡启动）。

    `query_fn` / `root_fn` 可注入 —— 测试不必真起子进程。
    """
    try:
        root = (root_fn or make_data_root_query(python, home))()
    except Exception as exc:                                # noqa: BLE001
        return [t("[settings] 数据根读取失败（%s）—— 跳过设置检查") % exc]
    path = Path(root) / _SETTINGS_REL
    run = query_fn or (lambda code: query(python, home, code))
    try:
        report = json.loads(
            run(_HEAL_CODE % (str(path), json.dumps(_SEED_OVERRIDES))))
    except Exception as exc:                                # noqa: BLE001
        return [t("[settings] 设置检查失败（%s）—— 继续启动") % exc]

    action = str(report.get("action") or "")
    lines: list[str] = []
    if action == "seeded":
        lines.append(t("[settings] 首次启动：已写入默认设置 %s") % path)
    elif action == "backup":
        lines.append(t("[settings] 设置文件读取失败，已备份到 %s，并按默认重建")
                     % (report.get("backup") or t("(未记下路径)")))
    elif action == "error":
        lines.append(t("[settings] 设置文件处置失败（%s）—— 继续启动")
                     % (report.get("error") or t("原因未知")))
    _, problems = read(path, load_baseline(home))
    lines.extend("[settings] " + p for p in problems)
    return lines
