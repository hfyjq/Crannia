"""`ultra` 档的工作流编排 —— 声明式 DAG，不执行模型写的代码。

## 它是什么

模型一次工具调用提交一张有依赖关系的步骤图，框架按波次并行跑子 agent，
只回一份汇总。它是 `ultra`（第 6 档）与 `xhigh` 的唯一区别。

同类实现里最接近的做法是"运行由模型编写的、扇出 subagent 的脚本"。
我们只搬形状，不搬执行方式：那需要一层沙箱运行时；
本仓没有、也不该为此造一层。我们收 JSON DAG。

## 三条判据

1. 子 agent 失败 ≠ 工作流失败：失败是逐条 null，
   该条自己的下游不跑、别的照跑；编排器本身不失败。
默认 `continue`：失败步记 `null` + 原因，依赖它的步跳过，其余照跑。
2. 引擎级故障才整张图停：参数/DAG 非法、超上限、用户取消。
3. 失败不会被当成成功：依赖失败的步骤不会拿空值硬算；报告里逐条写清。

## 重试

常见做法只有传输层重试（子 agent 是在重试之后才返回 null）；任务层没有。这里补一个显式的、有上限的：
每步 `retries` 0–2，默认 1；只重试「没做成」（失败/异常/空产出），
不重试取消与依赖失败；每次尝试都算进 32 的上限。

## 报告

无论成败，工具都正常返回（不是异常、也不终止这一轮）—— 主 agent 拿到报告后
可以自己补做、再派一次子 agent、或再跑一张图，而不是卡在失败上。
"""

from __future__ import annotations

from typing import Any

#: 一张图最多几步 —— 再多多半是模型不会拆（也防止一次调用烧穿账单）。
MAX_STEPS = 12

#: 同波最多并行几条。
MAX_PARALLEL = 35
DEFAULT_PARALLEL = 8

#: 每步最多重试几次（不含第一次）。
MAX_RETRIES = 2
DEFAULT_RETRIES = 1

#: 失败策略：`continue`（默认，失败步记 null、下游跳过、其余照跑）· `abort`（"这张图缺一块就不该交"）。
DEFAULT_ON_FAILURE = "continue"
_ON_FAILURE = frozenset({"continue", "abort"})

#: 每一步回给父的文本上限 —— 12 步 × 完整报告也要收得住。
STEP_OUTPUT_CAP = 4000

TOOL_NAME = "run_workflow"


class WorkflowError(ValueError):
    """这张图不能跑（参数 / 依赖 / 环 / 上限）—— 拒，并带路线。"""


def _as_int(value, default: int, *, low: int, high: int, name: str) -> int:
    """只收真整数（`True` 不是 1），范围外拒。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowError("%s 要是 %d–%d 的整数（收到 %r）"
                            % (name, low, high, value))
    if not (low <= value <= high):
        raise WorkflowError("%s 要在 %d–%d 之间（收到 %d）"
                            % (name, low, high, value))
    return value


def validate(steps, *, max_parallel=None, on_failure=None) -> dict:
    """校验并规范化一张图；返回 `{steps, max_parallel, on_failure}`。

    纯函数（零 IO、零副作用）—— 读者直接喂自造输入。
    """
    if not isinstance(steps, (list, tuple)):
        raise WorkflowError("steps 必须是数组（收到 %s）" % type(steps).__name__)
    if not steps:
        raise WorkflowError("steps 是空的 —— 至少给一步")
    if len(steps) > MAX_STEPS:
        raise WorkflowError("一张图最多 %d 步（收到 %d）—— 拆成两轮更好"
                            % (MAX_STEPS, len(steps)))

    policy = str(on_failure or DEFAULT_ON_FAILURE)
    if policy not in _ON_FAILURE:
        raise WorkflowError("on_failure 只能是 continue / abort（收到 %r）"
                            % (on_failure,))
    parallel = _as_int(max_parallel, DEFAULT_PARALLEL,
                       low=1, high=MAX_PARALLEL, name="max_parallel")

    norm: list[dict] = []
    seen: set[str] = set()
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise WorkflowError("第 %d 步不是对象（收到 %s）"
                                % (i + 1, type(raw).__name__))
        sid = str(raw.get("id") or "").strip()
        if not sid:
            raise WorkflowError("第 %d 步没有 id" % (i + 1))
        if len(sid) > 40:
            raise WorkflowError("步骤 id 太长（>40）：%r" % sid[:40])
        if sid in seen:
            raise WorkflowError("步骤 id 重复：%r" % sid)
        seen.add(sid)
        task = str(raw.get("task") or "").strip()
        if not task:
            raise WorkflowError("步骤 %r 没有 task" % sid)
        deps_raw = raw.get("depends_on") or []
        if not isinstance(deps_raw, (list, tuple)):
            raise WorkflowError("步骤 %r 的 depends_on 必须是数组" % sid)
        deps = []
        for d in deps_raw:
            ds = str(d or "").strip()
            if not ds:
                raise WorkflowError("步骤 %r 的 depends_on 里有空值" % sid)
            if ds == sid:
                raise WorkflowError("步骤 %r 依赖它自己" % sid)
            deps.append(ds)
        retries = _as_int(raw.get("retries"), DEFAULT_RETRIES,
                          low=0, high=MAX_RETRIES, name="retries(%s)" % sid)
        norm.append({"id": sid, "task": task, "depends_on": deps,
                     "retries": retries})

    ids = {s["id"] for s in norm}
    for s in norm:
        for d in s["depends_on"]:
            if d not in ids:
                raise WorkflowError("步骤 %r 依赖了不存在的 %r（现有：%s）"
                                    % (s["id"], d, ", ".join(sorted(ids))))

    plan = {"steps": norm, "max_parallel": parallel, "on_failure": policy,
            "waves": waves(norm)}
    return plan


def waves(steps: list[dict]) -> list[list[str]]:
    """拓扑分层 —— 同层内的步骤互不依赖，可以并行。

    环，`WorkflowError`（带剩下的节点，模型照着改）。
    """
    by_id = {s["id"]: s for s in steps}
    pending = {sid: set(s["depends_on"]) for sid, s in by_id.items()}
    done: set[str] = set()
    out: list[list[str]] = []
    order = [s["id"] for s in steps]          # 保持输入顺序（可复现）
    while pending:
        layer = [sid for sid in order
                 if sid in pending and not (pending[sid] - done)]
        if not layer:
            raise WorkflowError("这张图有环（剩下的：%s）"
                                % ", ".join(sorted(pending)))
        out.append(layer)
        for sid in layer:
            pending.pop(sid, None)
        done.update(layer)
    return out


def build_step_directive(step: dict, dep_outputs: dict, *,
                         attempt: int = 1, previous_error: str = "") -> str:
    """一步的 directive：四段式 + 依赖结果 + 重试说明。

    四段式本体不在本模块重写 —— 它只有一个产者（`subagent.build_directive`）。
    """
    from core import subagent as SA

    text = SA.build_directive(step["task"])
    if dep_outputs:
        blocks = []
        for sid, out in dep_outputs.items():
            body = str(out or "").strip()
            if len(body) > STEP_OUTPUT_CAP:
                body = body[:STEP_OUTPUT_CAP] + "\n…（已截断）"
            blocks.append('<step id="%s">\n%s\n</step>' % (sid, body))
        text += ("\n\n以下是**你依赖的那几步**的结果（只读参考，别重复做）：\n"
                 + "\n".join(blocks))
    if attempt > 1:
        why = str(previous_error or "").strip()
        if len(why) > 300:
            why = why[:300] + "…"
        text += ("\n\n这是**第 %d 次尝试**；上一次没做成：%s\n"
                 "⇒ 换一个做法再试，**别用同样的路子重来**。" % (attempt, why or "（没给原因）"))
    return text


def _clip(text: str, cap: int = STEP_OUTPUT_CAP) -> str:
    s = str(text or "")
    return s if len(s) <= cap else s[:cap] + "\n…（已截断）"


def format_report(steps: list[dict], *, on_failure: str,
                  cancelled: bool = False, aborted: bool = False) -> str:
    """把逐条结果排成给模型看的 JSON（工具回执，不套额外信封）。"""
    import json

    rows = []
    counts = {"completed": 0, "failed": 0, "skipped": 0, "cancelled": 0}
    for s in steps:
        status = str(s.get("status") or "")
        if status in counts:
            counts[status] += 1
        rows.append({
            "id": s.get("id"),
            "status": status,
            "attempts": int(s.get("attempts") or 0),
            "output": _clip(s.get("output") or ""),
            "error": _clip(s.get("error") or "", 800),
            "skipped_reason": _clip(s.get("skipped_reason") or "", 300),
        })
    ok = (counts["failed"] == 0 and counts["skipped"] == 0
          and counts["cancelled"] == 0 and not cancelled and not aborted)
    return json.dumps({
        "success": ok,
        "on_failure": on_failure,
        "cancelled": bool(cancelled),
        "aborted": bool(aborted),
        "counts": counts,
        "steps": rows,
        "note": ("失败/跳过都在这张表里 —— 你可以**自己补做**、"
                 "或者对某一步再派一次子 agent；不要假装它们成功了。"),
    }, ensure_ascii=False)


WORKFLOW_TOOL_SCHEMA: dict = {
    "description": (
        "把一件事拆成**有依赖关系的几步**，交给多个子 agent 并行做，只回一份汇总。"
        "★ 什么时候用（你自己判断，不用等用户点名）：任务有 3 个以上**互不依赖**的部分，"
        "或需要多角度交叉核对/对抗验证时。"
        "★ 别用它做：一两步能完成的事、需要逐步看结果再决定下一步的事 —— "
        "它一次就把整张图跑完，中间你看不到过程。"
        "每步写清要回什么（文件:行号 / 清单 / 结论）；依赖别的步骤就写 depends_on。"
        "★ 失败不会卡住整张图：失败步记 null、依赖它的跳过、其余照跑，"
        "报告里逐条列出来 —— 你拿到报告后可以自己补做或再派一次。"
        "★ 只在 ultra 档可用。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "description": "最多 12 步；每步 {id, task, depends_on?, retries?}",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "这一步的名字（图内唯一）"},
                        "task": {"type": "string", "description": "要它做什么（自足：子只有快照 + 这句话）"},
                        "depends_on": {
                            "type": "array", "items": {"type": "string"},
                            "description": "依赖哪些步骤的 id（它们的结果会附给你的 directive）",
                        },
                        "retries": {
                            "type": "integer",
                            "description": "失败后最多重试几次（0–2，默认 1）",
                        },
                    },
                    "required": ["id", "task"],
                },
            },
            "max_parallel": {
                "type": "integer",
                "description": "同波最多并行几条（默认 8，最大 32）",
            },
            "on_failure": {
                "type": "string", "enum": ["continue", "abort"],
                "description": ("continue（默认）= 失败步记 null、依赖它的跳过、其余照跑；"
                                "abort = 缺一块就不该交时用"),
            },
        },
        "required": ["steps"],
    },
}


__all__ = [
    "MAX_STEPS", "MAX_PARALLEL", "DEFAULT_PARALLEL", "MAX_RETRIES",
    "DEFAULT_RETRIES", "DEFAULT_ON_FAILURE", "STEP_OUTPUT_CAP", "TOOL_NAME",
    "WorkflowError", "validate", "waves", "build_step_directive",
    "format_report", "WORKFLOW_TOOL_SCHEMA",
]
