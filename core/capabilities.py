"""框架向 skill 声明的能力面。

## 为什么要有它

一个 skill 可能用到"某一版框架才刚有的东西"（比如产物能内联渲染、后台任务表）。
老版本框架上装一个新 skill，最坏的结果不是装不上，而是装上、一半坏 ——
工具在、调了报错，或者工具干脆不出现，而没有任何人说过为什么。

两条约定：

  * skill 在 `manifest.yaml` 里写 api: <整数> 与 requires: [<能力名>…]；
  * 框架在这里声明自己支持什么；缺任何一项，不装载，并给出一句可展示的原因。

单向兼容是刻意的：不承诺"旧框架跑新 skill"（做不到，硬做出来是漏洞面）。
承诺的是 它不半残、并且说得出为什么 —— 老版本用户因此知道"要更新"，
而不是得到一堆莫名其妙的行为。

## 加一个能力

在这里的 `CAPABILITIES` 里加一行（唯一来源）+ 在版本说明里写明它从哪一版起可用。
加能力不算破坏性变更，`API_VERSION` 不用动；只有删/改已有能力的语义才 +1。
"""

from __future__ import annotations

from typing import Any

__all__ = ["API_VERSION", "CAPABILITIES", "gate", "missing_capabilities",
           "optional_missing"]

#: 契约版本。只在破坏性变更时 +1。
API_VERSION = 1

#: 框架支持的能力名（唯一来源）。
CAPABILITIES: frozenset[str] = frozenset({
    # 工具 / 模型产出的图、音、视频进同一条呈现链（`core/artifacts.py`）
    "artifacts",
    # 产物记录带 `inline`（超内联闸时给下载行而不是硬塞 <img>/<video>）
    "artifacts.inline",
    # 后台任务表（`core/jobs.py`）
    "jobs",
    # 落点与查找（`core/workspace.py`）
    "workspace",
    # 界面文案（`core/i18n.py`）
    "i18n",
    # 技能的框架上下文（core/extension.py）
    "context",
    # 键值存储 ctx.store
    "storage",
    # 技能的后台任务 ctx.jobs。jobs 表示框架提供任务表，jobs.skill 表示技能可以提交任务
    "jobs.skill",
    # 技能的长期记忆接口 ctx.memory
    "memory",
    # 技能的模型调用接口 ctx.llm
    "llm",
    # 技能在工具里向用户申请一次授权 ctx.approval
    "approval",
})


def missing_capabilities(requires: Any) -> list[str]:
    """把 manifest 的 `requires` 归一成"缺哪些"，排序确定（日志/界面要稳）。"""
    if not requires:
        return []
    if isinstance(requires, str):
        requires = [requires]
    if not isinstance(requires, (list, tuple, set, frozenset)):
        return []
    return sorted({str(x).strip() for x in requires if str(x).strip()} - CAPABILITIES)


def optional_missing(manifest: Any) -> list[str]:
    """`optional` 里本框架没有的能力 —— 不阻止装载，但要记下来。

    与 `requires` 的分工：`requires` 缺一项 = 装了也只能半残，不装；
    `optional` 缺一项 = 技能自己写好了降级路（`ctx.has(...)` 分支），装，
    但装载报告里要留一行，好让人知道"它是降级跑着的"。
    """
    if not isinstance(manifest, dict):
        return []
    return missing_capabilities(manifest.get("optional"))


def gate(manifest: Any) -> str:
    """这个 manifest 能不能装载；能装，空串，不能，一句给人看的原因。

    判据只读 manifest，不碰磁盘、不 import handler —— 它要能在装载之前回答，
      而且它自己不许抛（一个坏 manifest 不该让整个技能目录消失）。
    """
    if not isinstance(manifest, dict):
        return "manifest 不是一个对象"

    api = manifest.get("api")
    if api is not None:
        # `isinstance(True, int)` 是 True，`api: true` 会被当成 1（同类的坑）
        if isinstance(api, bool) or not isinstance(api, int):
            return "api 必须是整数（收到 %r）" % (api,)
        if api > API_VERSION:
            return ("需要框架更新：这个技能要求 API %d，本框架支持到 %d"
                    % (api, API_VERSION))

    missing = missing_capabilities(manifest.get("requires"))
    if missing:
        return "需要框架更新：缺少能力 %s" % "、".join(missing)
    return ""
