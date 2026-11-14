"""Excel 数据验证（下拉列表 / 数值区间 / 日期 …）的读取侧。

## 这个文件补的是一个静默空转

`read_excel_data(include_validation=True)` 里逐格 import 的就是本模块的
`get_data_validation_for_cell`。若这个模块缺失、异常又被一句裸
`except Exception` 吞掉：

    cell_data["validation"] = {"has_validation": False}

`include_validation=True` 不会返回任何真规则，而且
没有任何东西会告诉你 —— 它给的是一个看起来像结论的 `False`。

这与写入侧的 `add_excel_data_validation` 是一对：那边能写、这边必须读得回来。

## 两条判据（别改）

1. 读不到 ≠ 没有。「这个格子没有验证规则」与
   「我没查出来」是两件事，前者回 `None`，后者抛出去让调用方记成
   `has_validation=None` + `error`。不许静默塌成 `False`。
2. 本模块只读，不碰 `wb.save` / 任何写标记（`read` 档的实现不许含写标记，
   `tests/test_permission_tiers.py` 有 AST 读者）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from openpyxl.utils.cell import column_index_from_string, coordinate_from_string


def get_data_validation_for_cell(
    worksheet, cell_address: str
) -> Optional[Dict[str, Any]]:
    """`cell_address`（如 `"C2"`）上的验证规则。

    * 有，返回规则字典（`has_validation: True` + 类型 / 取值 / 提示语）
    * 没有，返回 `None`
    * 查不出来，抛异常（调用方记成未知，不许塌成 False）
    """
    col_letter, row = coordinate_from_string(cell_address)
    col_idx = column_index_from_string(col_letter)

    for dv in worksheet.data_validations.dataValidation:
        if _covers(row, col_idx, dv):
            return _extract(dv, cell_address, worksheet)
    return None


def _covers(row: int, col: int, dv) -> bool:
    """这个格子在不在这条规则的 `sqref` 覆盖范围里。"""
    for rng in dv.sqref.ranges:
        if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
            return True
    return False


def _extract(dv, cell_address: str, worksheet) -> Dict[str, Any]:
    """把一条 `DataValidation` 摊成给模型看的字典。

    取不到的字段不写，而不是写一个空串 —— 空串看起来像"这条规则就是空的"。
    """
    info: Dict[str, Any] = {
        "cell": cell_address,
        "has_validation": True,
        "validation_type": dv.type,
        "allow_blank": bool(dv.allowBlank),
    }
    if dv.operator:
        info["operator"] = dv.operator
    if dv.prompt:
        info["prompt"] = dv.prompt
    if dv.promptTitle:
        info["prompt_title"] = dv.promptTitle
    if dv.error:
        info["error_message"] = dv.error
    if dv.errorTitle:
        info["error_title"] = dv.errorTitle

    if dv.type == "list" and dv.formula1:
        info["allowed_values"] = _list_values(dv.formula1, worksheet)
    elif dv.formula1:
        info["formula1"] = dv.formula1
        if dv.formula2:
            info["formula2"] = dv.formula2

    return info


def _list_values(formula: str, worksheet) -> List[str]:
    """把 `list` 类型的 `formula1` 摊成候选值。

    两种写法都认（与 `add_excel_data_validation` 写出去的形状一致）：
      * 字面选项 "合格,不合格" —— 逗号分隔
      * 区域引用 `=$E$1:$E$5` —— 去表里把值读出来
    """
    raw = str(formula).strip()
    # 去掉包裹的双引号；openpyxl 存字面选项时要求带引号
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1]

    # 字面选项：逗号分隔
    if ":" not in raw and not raw.startswith("$") and not raw.startswith("="):
        return [v.strip() for v in raw.split(",") if v.strip()]

    # 区域引用：去表里读
    ref = raw[1:] if raw.startswith("=") else raw
    try:
        cells = worksheet[ref]
    except Exception:
        # 区域解不开时不假装空：把引用原样回给模型，让它自己判断
        return [f"范围: {ref}（解析不了，请自行查看）"]

    flat = []
    if hasattr(cells, "value"):            # 单个格子
        if cells.value is not None:
            flat.append(str(cells.value))
    else:
        for item in cells:
            if hasattr(item, "value"):     # 一维
                if item.value is not None:
                    flat.append(str(item.value))
            else:                          # 二维
                for c in item:
                    if c.value is not None:
                        flat.append(str(c.value))
    return flat if flat else [f"范围: {ref}（里头没有值）"]
