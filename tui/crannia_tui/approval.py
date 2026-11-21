"""审批载荷的形状：把后端给的结构拆成 `Prompt` / `Option`。

界面那一层在 `ui.ApprovalPanel`（全屏版）—— 这里只管"后端说了什么"。

**这一层不判断"该不该批"**。它只做三件事：把后端给的结构摆出来、把用户选的
那个 key 原样送回去、把失败说清楚。授权语义（"本会话不再询问"到底意味着什么）
全在后端 —— 在这里再实现一遍，两边的口径迟早会分叉。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class Prompt:
    """一次待回答的请求。字段与后端的载荷一一对应。"""

    request_id: str
    kind: str = ""
    body: str = ""
    options: tuple[Option, ...] = ()
    default_key: str = ""
    #: 非空表示"中间态"：这一步该做的不是选一项，而是说一句话。
    #: 后端那时不给选项表 —— 给了也是没人读的死数据。
    expect: str = ""
    multi: bool = False
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, raw: dict) -> "Prompt | None":
        if not isinstance(raw, dict):
            return None
        rid = raw.get("request_id")
        if not isinstance(rid, str) or not rid:
            return None
        opts = []
        for item in raw.get("options") or ():
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if not key:
                continue
            opts.append(Option(key=key,
                               label=str(item.get("label") or key),
                               description=str(item.get("description") or "")))
        return cls(
            request_id=rid,
            kind=str(raw.get("kind") or ""),
            body=str(raw.get("body") or ""),
            options=tuple(opts),
            default_key=str(raw.get("default_key") or ""),
            expect=str(raw.get("expect") or ""),
            multi=bool(raw.get("multi")),
            extra={k: v for k, v in raw.items()
                   if k not in ("request_id", "kind", "body", "options",
                                "default_key", "expect", "multi")},
        )

    @property
    def wants_text(self) -> bool:
        return self.expect == "text" or not self.options

    def keys(self) -> list[str]:
        return [o.key for o in self.options]

    def key_for_number(self, raw: str) -> str | None:
        """把用户敲的序号换成选项 key。

        用户看到的是 1/2/3 这样的序号，而 key 是后端定的（可能不是数字）。
        只按位置映射，不去猜 key 的含义。
        """
        if not raw.isdigit():
            return None
        idx = int(raw) - 1
        if 0 <= idx < len(self.options):
            return self.options[idx].key
        return None
