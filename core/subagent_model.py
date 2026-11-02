"""子 agent 用哪个模型。

可选项 = 端配置页里用户自己建的配置条目（含中转站），
而不是 `providers/` 下的目录名（供应商级）。
用户能看到、能选的就是他在端配置页自己建的那些。

它不进档表：子 agent 用哪个模型不是
投入档那两格（子 agent 个数 + 思考模式）之一，它是独立设置。
判据是档表的分界线：「档位管能力与代价……不管用户口味」——
选哪个模型更像成本偏好。

## 空值 = 「跟随当前这一段」，不是「跟随默认」

本模块实现的是跟随当前会话（空串 = 沿用父那一轮的 provider）。两条理由：

  ① 那是既有行为 —— 换粒度这一批不该顺手改语义；
  ② 更硬的一条：子 agent 是从父那条 assistant fork 出来的
     （`tools` / `system` / `session_id` 三者都取自快照，**判据是
     "子与父同前缀"**）。父这一段冻的是 A，而"默认"是 B，子会拿着
     另一个模型去续一个按 A 建的上下文，而用户什么都没做。

这是有意偏离的一处，如实记在这儿
  （界面文案必须跟着实际行为说 —— 设置页那一句写的是「沿用当前会话的模型」）。

读不到，`""`（= 沿用会话那个模型，也是默认 —— 多数人不会配它）。
  读到了但用不了，由调用方拒并带路线（`agent_loop` 会回一句人话 +
  出路；拒绝要带路线），不许静默退回会话模型
  （那是"设置页写着 A、实际跑 B"，「标签在说谎」的形状）。

老值（供应商目录名）不是"静默忽略"：旧版存的是 `anthropic`
  这种目录名，而今天这一格要的是 `m3f8a1c2` 这种条目 id，老值必然解析不到。
  「静默忽略一个用户改过的键比不许改更坏」
`legacy_note()` 就是为它准备的：调用方（设置页 GET）把它说出来。
"""

from __future__ import annotations

import json
from pathlib import Path

#: 相对路径 —— `tests/_sandbox.py::sandbox()`
#: 靠 chdir 生效，写绝对路径就等于测试去动生产数据。
_SETTINGS_PATH = Path("data/settings.json")

#: 落 `data/settings.json` 的那个键。空串 = 沿用会话模型。
#: 键名保持 `subagent_model`（换了旧值就成了没人读的死键）——
#:   换的是值的语义，而语义漂了这件事由下面的 `legacy_note()` 说出来。
SETTING_KEY = "subagent_model"


def available() -> list[dict]:
    """能挑的那些 —— 端配置页里用户自己建的配置条目。

    回 `[{id, name, card, card_label, model}]`（不是一串裸 id）：
    界面上那一格要显示"人认得的名字"，而只给 id 的话用户看到的就是 `m3f8a1c2`。

    可选项与"实际能装上的"同源：这里直接读
      `core.model_configs` 那个账本 —— 端配置页里列得出来的，
      这里一定列得出来，反之亦然；不另扫一遍 `providers/` 目录
      （那会是第二份真相源）。
    排序是确定的（卡序 + 卡内建序）—— 下拉框的顺序不该每次刷新都跳。
    """
    try:
        from core import model_configs as _mc
        #: 形状只有一处（`names_payload()`）—— 群成员那个模型下拉走同一个函数。
        return _mc.names_payload()
    except Exception:                                           # noqa: BLE001
        return []


def ids() -> list[str]:
    """只要 id —— 判"这个值认不认识"的地方用它（`web/server.py` 那条校验）。"""
    return [o["id"] for o in available()]


def configured_model(settings: dict | None = None) -> str:
    """用户配的那个配置条目 id。读不到，`""`（= 沿用当前会话的模型）。

    *settings* 给了就用它（`/api/settings` 那条路手上已经有一份，别再读一次盘）；
    没给，自己读 `data/settings.json`（子 agent 派发那条路）。
    """
    if settings is None:
        try:
            if not _SETTINGS_PATH.exists():
                return ""
            settings = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:                                       # noqa: BLE001
            return ""          # 文件坏了，这属于「读不到」，默认（沿用会话）
    if not isinstance(settings, dict):
        return ""
    return str(settings.get(SETTING_KEY) or "").strip()


def legacy_note(value: str) -> str:
    """这个值认不出来时的一句人话（认得出来，`""`）。

    它存在的唯一理由是那条规矩：静默忽略一个用户改过的键，比不许改更坏。
      旧版存的是供应商目录名
      （`anthropic` / `qwen`…），而今天这一格要的是配置条目 id
老值必然解析不到，而不说不出来的话，用户看到的就是
      "我明明配了子 agent 模型、它却不生效"。
    """
    v = str(value or "").strip()
    if not v:
        return ""
    try:
        from core import model_configs as _mc
        if _mc.get(v):
            return ""
    except Exception:                                           # noqa: BLE001
        return ""
    return (f"子 agent 那个设置里存的还是老值「{v}」—— 它指的是**供应商目录名**"
            f"（旧版本的口径），而现在这一格要的是**端配置页里的一条配置**。"
            f"去「设置 → 对话设置」重选一个（或者清空 = 沿用当前会话的模型）。")
