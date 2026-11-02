"""Excel 工具的内部管道 —— 安全接线（步 3）+ 原子写与流式（步 4/6 起）。

这个模块里不放任何对外工具。工具住在 `excel_tools.py` /
`excel_query.py` / `excel_transform.py`，这里只放它们共用的那几件。
判据：bus 的发现走 `getattr(handler_module, name)`，所以**多一个公开函数
就等于多一个必须登记的入口**（`handler.py` 的 re-export 名单 + manifest）——
内部件不往那两处放。

为什么要有这个模块（而不是继续让 `excel_tools.py` 自己扛）：
路径解析表同时是安全校验点。把它单独放一处，是为了让
「解析 + 过核」这两个动作只有一个落点（一个判定两处会漂）。
"""

from __future__ import annotations

import os
import re
import time
import zipfile
from pathlib import Path

#: 落点口径只有一处 —— 见 `core/workspace.py` 那段。
from core.workspace import primary_output_dir

#: 安全核 —— 由 `handler.init()` 转调 `init()` 注入（同 `word_tools`）。
#: `skill_loader` 调的是 handler 的 `init`（子模块它不认识），
#: 所以那个 `init` 里必须多一行 `excel_tools.init(security)`，否则这里恒为 None
#:`_guarded` 每次调用都 raise（fail-closed，症状是"所有 Excel 工具全挂"，
#: 一眼可辨 —— 比静默放行好）。
_security = None


def init(security) -> None:
    """注入安全核（由 `handler.init()` 转调）。"""
    global _security
    _security = security


def _guarded(resolved: Path, op: str) -> Path:
    """按 *op* 过安全核（fail-closed）。

    没注入安全核就 raise，与 `handler._require_security()` 同一条规矩。
    直觉会是"没核就放行、保持兼容"—— 而那正是这个 bug 的形状：
    放行是静默的，拒绝才看得见。
    """
    if _security is None:
        raise RuntimeError(
            "SecurityGuard not initialized for excel_tools. "
            "skill_loader must call handler.init() before any Excel tool runs.")
    guarded = Path(_security.validate_path(str(resolved), op))
    if op == "read":
        # 产物侧的读态记账。挂在这个收口而不是逐个
        #   读工具里 —— 它是 Excel 全族的共同入口，一处覆盖 30 个调用点。
        #   记的是"解析过这个路径"而非"内容真交出去了"，极少量过度记账
        #   （解析成功但随后读失败），方向偏松（少拦一次），有意选的。
        #   实现与理由见 `core/file_tools.note_observed`（同 `word_tools._note_observed`）。
        try:
            from core.file_tools import note_observed
            note_observed(guarded)
        except Exception as exc:                              # noqa: BLE001
            print("[office] 读态记账失败（不影响读取）：%s: %s"
                  % (type(exc).__name__, exc))
    return guarded


#: 只读语义的 op —— 与 `SecurityGuard.validate_path` 的 Level 1 白名单同一份
#: （那里写着"只读路径只放 read 和 copy"）。它决定要不要建输出目录。
_READ_OPS = ("read", "copy")


def guarded_resolve(file_path: str, op: str = "write") -> Path:
    """解析路径 + 过安全核。Excel 全部工具的唯一收口。

    ## `op` —— 每个调用点都要显式写出来（同 `word_tools`）

    | 语义 | 件数 | 例 | op |
    |---|---|---|---|
    | 纯读 | 4 | `get_workbook_metadata` · `read_excel_data` · `validate_formula_syntax` · `get_merged_cells` | `"read"` |
    | 其余（写） | 20 | `write_excel_data` · `apply_formula` · `format_excel_range` · `delete_*` … | `"write"` |

    不能在这一处统一写成 `"write"` —— 那会把「读桌面上那份 xlsx」也拒掉，
    而那是正当用法（`read_excel_data` 读跨区文件，`READ_ONLY` 只放 read/copy）。
同一条判据：两类语义，不能一把 sed 全换。

    每一处显式写、不靠默认值 —— 默认 `"write"` 只是给将来新加的函数
    兜底，方向 fail-closed：漏写，多拒（看得见），不会 少拒（看不见）。
    机械读者：`tests/test_office_guard.py`（按 AST 反查每一处都有显式 op）。

    这里是那条硬约束的落点：Excel 工具不能跳过 `validate_path`，
    而 `_resolve_xlsx_path` 对绝对路径原样放行
`write_excel_data("/任意位置/x.xlsx", …)` 能写出去 ——
    违反「`validate_path()` 在 `open()` 之前」。同族：`word_tools` 也遵守这一条。

    ## `op` 还决定"要不要建输出目录"

    相对路径要先拼到输出根上，而那个根可能还不存在。以前这一行无条件建目录，
    于是声明 `read` 的工具第一次被调用时会创建一个空目录 —— 文件内容一个字节
    都没改，但它确实是写副作用，而 `read` 那一档的契约是"可以并发跑、
    可以被只读子 agent 放行"。

    现在只在 `op` 不属于 `_READ_OPS` 时建。
    """
    p = Path(file_path)
    if p.is_absolute():
        resolved = p
    else:
        # 已经是 workspace/... 开头 → 不要再拼一次
        # （这两行保持 `_resolve_xlsx_path` 的逐字语义 ——
        #   只加围栏，不改解析语义）
        normalized = str(p).replace("\\", "/")
        if normalized.startswith("workspace/"):
            resolved = Path(normalized).resolve()
        else:
            workspace = primary_output_dir()
            if op not in _READ_OPS:
                workspace.mkdir(parents=True, exist_ok=True)
            resolved = (workspace / p).resolve()
    return _guarded(resolved, op)


# 原子写 + 回读校验
#
# 为什么不能继续用裸 `wb.save(str(path))`：
#   · 大表保存要好几秒（openpyxl 会把整本重新序列化 + 重压缩）。
#     中途失败 = 盘上是一份写到一半的新内容，而旧的原件已经没了。
#   · 保存"看起来成功了"但盘上是旧的/半截的 —— 没有任何东西会告诉你。
#判据：改数据这件事不许静默地做错。
#
# 临时文件必须与目标同目录：`os.replace` 只有在同一卷上才是原子的，
#    而系统临时目录常常在另一个盘（本仓就横跨 `C:`/`D:`）。
#
# 本模块不走 `guarded_resolve`：调用方（各工具）已经在入口处过过核了，
#    这里拿到的 `path` 是核过之后的那个对象。再核一次会变成两处判定。

_LVTMP_SUFFIX = ".lvtmp"

#: `os.replace` 的重试。总尝试次数与每次失败后的等待基数。
#:
#: 为什么必须重试：Windows 上 `os.replace` 在目标文件被瞬时占用时抛
#:   `PermissionError [WinError 5]` —— 占用者通常是杀毒软件 / 索引器，
#:   不是我们自己的句柄（我们已经在下面 `wb.close()` 过了）。
#:   `tests/test_excel_read.py` 有 2 条间歇性红（连跑三次 `61/2红` · `63/0` · `63/0`），
#:   真错误就是这一步。
#: 它不是"测试不稳"那么轻：任何走 `save_workbook` 的写工具都会间歇性写失败
#:   （用户侧看到的是"偶发保存失败"）—— 而 `save_workbook` 是全部 30 处 Excel 写盘的收口
#:   （`excel_tools.py` 22 + `excel_transform.py` 8）。
#: 重试只在 Windows 上值得付：POSIX 的 `os.replace` 是原子的、不会这样失败。
_REPLACE_RETRIES = 5
_REPLACE_BACKOFF_S = 0.06


def _replace_with_retry(tmp: Path, path: Path) -> None:
    """`os.replace(tmp, path)`；对瞬时占用重试几次，最后一次不接（失败就抛）。

    判据不许放宽成"重试到成功为止" —— 那会在真正写不进去时永远转下去。
    次数封顶 + 最后一次照抛，是这里唯一安全的方向。
    """
    for attempt in range(max(1, int(_REPLACE_RETRIES)) - 1):
        try:
            os.replace(str(tmp), str(path))
            return
        except OSError:
            time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))
    os.replace(str(tmp), str(path))


def _verify_xlsx(path: Path, expected_sheets: list) -> str:
    """回读校验：只看 zip 目录与 `xl/workbook.xml`，不把表体读进来。

    大表上这一步必须是 O(1) —— 否则"校验"本身就成了第二遍全量读，
    而那正是压力最大时最容易踩的坑（一本几十万行的表读两遍）。
    zip 的中央目录在文件末尾，读一个小成员 = 一次 seek + 少量解压，
    与表体大小无关。

    返回错误串（空串 = 通过）。
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "xl/workbook.xml" not in names:
                return "写出来的不是有效 xlsx（缺 xl/workbook.xml）"
            xml = z.read("xl/workbook.xml").decode("utf-8", "replace")
    except Exception as e:                      # noqa: BLE001
        return "写出来的文件打不开：%s: %s" % (type(e).__name__, e)

    got = len(re.findall(r"<sheet\b", xml))
    want = len(expected_sheets)
    if got != want:
        return "表数对不上：写出 %d 张，期望 %d 张" % (got, want)
    return ""


def save_workbook(wb, path: Path) -> str:
    """原子写 + 回读校验。返回错误串（空串 = 成功）。

    调用方拿它当 {"success": False, "error": ...} 用 ——
    不许静默忽略返回值：那等于把刚加上的回读校验又关掉。
    """
    sheets = list(wb.sheetnames)
    tmp = path.with_name(path.name + _LVTMP_SUFFIX)
    try:
        wb.save(str(tmp))
    except Exception:
        # 半截的临时文件不许留在盘上（它和真文件同名同目录，最容易被误认）
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    finally:
        # 顺序：`close()` 在 `os.replace` 之前 —— Windows 上被打开的
        #    文件不许替换。`read_only=False` 时 close 是空操作（openpyxl 文档写明），
        #    所以这一步对绝大多数站点无害、对被扩展成流式的那些是必需的。
        wb.close()

    err = _verify_xlsx(tmp, sheets)
    if err:
        try:
            tmp.unlink()
        except OSError:
            pass
        return err

    try:
        _replace_with_retry(tmp, path)
    except OSError as exc:
        # 返回错误串，不抛 —— 这是本函数的既有契约（见 docstring：
        #   *"调用方拿它当 `{"success": False, "error": ...}` 用"*）。
        #   抛出去只会被调用方最外层的 `except` 接住，消息里就丢了
        #   "是换名这一步失败的"这一半（20 处调用点全是同一种接法）。
        try:
            tmp.unlink()
        except OSError:
            pass
        return ("换名失败（目标文件可能被其它程序占用）: %s: %s"
                % (type(exc).__name__, exc))
    # 写成功之后回记一笔。这里是 Excel 全部写盘的唯一收口
    #   （30 个调用点），所以一处覆盖全族 —— 模型自己刚写过的表就是"熟脸"，
    #   下一次写不会被 `overwrite_guard` 误判成"没读过"或"版本对不上"。
    #   （`file_tools.py:676` 同一形状。）
    _note_observed(path)
    return ""


def overwrite_blocked(path: Path, *, reader: str = "read_excel_data"):
    """整块覆盖一个可能已存在的 .xlsx 之前的那一句问话。

    见 `core/file_tools.overwrite_guard` —— 判据：
    不存在，放行 · 没读过，拒 · 读过但变了，拒 · 读过且没变，覆盖。
    返回 `None` = 放行；否则 `{"error","hint"}`。

    只挂"整块覆盖"类工具（`write_excel_data` / `merge_excel_workbooks`）——
      `format_excel_range` / `insert_excel_rows` 这类自己 load 新鲜版本再改
      的不挂（界线出处同 `word_tools._overwrite_blocked` 的 docstring）。
    异常一律放行并出声：这是检测不是边界，别让它因为自身出错拦住活。
    """
    try:
        from core.file_tools import overwrite_guard
        return overwrite_guard(path, reader=reader)
    except Exception as exc:                                  # noqa: BLE001
        print("[office] 覆盖闸出错（已放行）：%s: %s" % (type(exc).__name__, exc))
        return None


def _note_observed(path) -> None:
    """写成功之后的读态回记 —— 见 `core/file_tools.note_observed`。"""
    try:
        from core.file_tools import note_observed
        note_observed(path)
    except Exception as exc:                                  # noqa: BLE001
        print("[office] 读态记账失败（不影响写入）：%s: %s"
              % (type(exc).__name__, exc))


# 块读取（按真实表 `AAA公司…经营总结.xlsx` 的判据定）
#
# 真实表照出来四件事，这一段就是对着它们写的：
#   ① 跨表：两个基准题都是「同一块区域 × 9 张表」（`sheet_name="*"`）
#   ② 跨表公式：`F54 = ='8月'!E54` · `I7 = =+'8月'!I7+E7` —— 累计/环比全靠它
#不给缓存值，模型看到的是公式串，经营总结里最要紧的派生指标一个都读不到
#   ③ 假空：266 个合并区产生 1253 格"看起来是空的"（纵向合并的分组标签只在左上角）
#   ④ 一屏读得下：价格块 70 格 ≈ 4,400 字符 —— 这张表用不着分页
#
# 流式与合并/校验互斥：`read_only=True` 的 sheet 没有
# `merged_cells`、也没有 `data_validations`（只有 `iter_rows`）。
#需要"铺开合并区"或"读校验规则"时必须走非流式；其余一律流式。

from openpyxl.utils import get_column_letter   # noqa: E402

#: 没给 `end_cell` 时的行数安全上限。
#: 没有它，"自动扩展到数据区域"在 10 万行的表上会先建 200 万个 dict ——
#: OOM 发生在 6000 字符那道闸之前（闸管的是输出，管不了我们自己的内存）。
READ_SAFETY_ROWS = 5000

#: 超过这么多格就不做"公式标记"的第二遍加载（大表上那是第二遍全量读）。
FORMULA_SCAN_CELL_CAP = 20000

FORMULA_MODES = ("auto", "cached", "formula")


def pick_sheet_names(all_names: list, sheet_name) -> tuple:
    """把 `sheet_name` 摊成要读的表名列表。返回 `(names, err)`。

    * `""` / `None`，第一张表（保持既有语义）
    * `"*"`，全部表（跨表读的入口）
    * `[..]`，按给的顺序（点名的那几张）
    * 其它字符串，就那一张（不存在则报错）
    """
    names = [str(n) for n in all_names]
    if isinstance(sheet_name, (list, tuple)):
        want = [str(s) for s in sheet_name]
        missing = [s for s in want if s not in names]
        if missing:
            return [], "工作表不存在: %s（可用: %s）" % (missing, names)
        return want, ""
    s = str(sheet_name or "")
    if s == "*":
        return names, ""
    if not s:
        return (names[:1], "") if names else ([], "工作簿里没有工作表")
    if s not in names:
        return [], "工作表 '%s' 不存在（可用: %s）" % (s, names)
    return [s], ""


def merge_anchors(ws) -> dict:
    """`{(row, col): (锚点地址, 锚点值)}` —— 只收非锚点的成员格。

    用来把"假空"填回来：纵向合并的分组标签只在左上角那一格，
    而模型逐格读时，其余格子全是 `None`（1253 格）。
    """
    out = {}
    for rng in ws.merged_cells.ranges:
        anchor_addr = "%s%d" % (get_column_letter(rng.min_col), rng.min_row)
        anchor_val = ws.cell(row=rng.min_row, column=rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if (r, c) != (rng.min_row, rng.min_col):
                    out[(r, c)] = (anchor_addr, anchor_val)
    return out


def read_block(ws_vals, ws_fml, *, min_row, max_row, min_col, max_col,
               keep_cols=None, merge_mode="raw", anchors=None,
               offset=0, limit=None, formula_marks=True) -> tuple:
    """一个矩形块 → `(rows, cols_kept)`；每格一个 dict（保持既有形状）。

    * `ws_vals` —— `data_only=True` 的那本（拿缓存值）
    * `ws_fml`  —— `data_only=False` 的那本（拿公式串）；不需要标记时传 `None`
    * `merge_mode="fill"` —— 非锚点成员格回填锚点值并标注 `merged_from`
    * `offset` / `limit` —— 分页：从块的第几行开始、最多取几行。
      `limit` 与 `max_row` 是两件事：前者是"这次要几行"（分页），
      后者是"这块区域到哪为止"。把它俩混成一件，`offset` 就会和它互相抵消
      （踩过：`offset=4, max_rows=4` 第二页直接回空）。

    走 `iter_rows` 而不是 `ws.cell(r,c)`：`read_only=True` 下后者不可靠，
    而流式是"大表不许物化"那条硬要求的实现方式。
    """
    keep = None if not keep_cols else {str(k).upper() for k in keep_cols}
    start = min_row + max(0, int(offset or 0))
    if start > max_row:
        return [], []
    cap = None if limit is None else max(0, int(limit))

    it_vals = ws_vals.iter_rows(min_row=start, max_row=max_row,
                                min_col=min_col, max_col=max_col,
                                values_only=True)
    it_fml = None
    if formula_marks and ws_fml is not None:
        it_fml = ws_fml.iter_rows(min_row=start, max_row=max_row,
                                  min_col=min_col, max_col=max_col,
                                  values_only=True)

    rows = []
    cols_kept = None
    for i, vrow in enumerate(it_vals):
        r = start + i
        frow = next(it_fml, None) if it_fml is not None else None
        cells = []
        for j in range(len(vrow)):
            c = min_col + j
            letter = get_column_letter(c)
            if keep is not None and letter not in keep:
                continue
            val = vrow[j]

            formula = None
            if frow is not None and j < len(frow):
                fv = frow[j]
                if isinstance(fv, str) and fv.startswith("="):
                    formula = fv

            merged_from = None
            if anchors:
                hit = anchors.get((r, c))
                if hit:
                    merged_from, anchor_val = hit
                    if val is None:
                        val = anchor_val

            cell = {"address": "%s%d" % (letter, r), "value": val,
                    "row": r, "column": c}
            if formula is not None:
                cell["formula"] = formula
                if val is None:
                    # `auto` 的落点：缓存值没有（没在 Excel 里开过，或我们
                    #   自己写的公式），把公式串交出去并标记它没值，
                    #   而不是给一个看起来像"这格是空的"的 `None`。
                    cell["value"] = formula
                    cell["formula_uncached"] = True
            if merged_from is not None:
                cell["merged_from"] = merged_from
            cells.append(cell)

        if cols_kept is None:
            cols_kept = [cc["address"].rstrip("0123456789") for cc in cells]
        rows.append(cells)
        if cap is not None and len(rows) >= cap:
            break
    return rows, (cols_kept or [])
