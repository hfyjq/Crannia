"""把测试的文件写入关进临时目录 + 跑完核对生产目录没被动。

为什么共享一个模块：同一份名单只许有一处定义，否则会各自漂移。

用法::

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ 自己
    from _sandbox import sandbox, prod_snapshot, prod_diff

    before = prod_snapshot()
    with sandbox():
        ...会写文件的用例...
    leaked = prod_diff(before, prod_snapshot())
    assert not leaked, leaked

直接跑 `python tests/test_x.py` 时 `sys.path[0]` 就是 `tests/`，
所以多数情况下这一行 import 可以直接写（上面那段 insert 是为了显式一点）。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import tempfile
from pathlib import Path


def tempdir(prefix: str = "lv_tmp_") -> Path:
    """`mkdtemp` 的清理版：进程退出时自动删掉。

    裸 `mkdtemp` 永远不清理 —— 跑一次全量就在 `%TEMP%` 里留下几百个空壳
    （实测累积到四千多个）。`TemporaryDirectory` 要配 `with`，而这些夹具的
    寿命跨函数（有的在 `__exit__` 里才用得到），所以用 `atexit`：
    中途抛异常也收得掉。
    """
    d = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, d, ignore_errors=True)
    return d

#: 临时目录里要预先造好的相对目录。
#: 用相对的默认路径，是因为被测代码用的就是相对默认值 ——
#: `DocSessionStore.output_dir="workspace"`、skill 自己的数据库路径
#: （`data/runtime/skills/...`）、`MemoryManager.storage_path="data/memory"`
#: —— 全是相对路径。

#: `data/runtime` 收纳校准器 / 跨区缓存 / chroma / skill 数据 / 摘要缓存。
#:新增一个"生产会写的目录"必须同时加到这里，否则机检看着绿。
#: `data/attachments` —— 归一化后的图片落在那里，
#: 而它不在这份名单里的话，任何"忘了沙箱"的用例都会静默地往生产目录写图
#: （`prod_snapshot` 只遍历这几个根），又一次「一边漏检、一边报干净」。
DEFAULT_DIRS = ("workspace", "data/memory", "logs", "data/workdata",
                "data/runtime", "data/attachments")


@contextlib.contextmanager
def sandbox(dirs=DEFAULT_DIRS):
    """chdir 进一个临时目录，并在里面造好 *dirs*。

    为什么非 chdir 不可：上面那些默认值全是相对路径，不换 cwd 的话
    它们照旧解析到生产目录 —— 不 chdir 等于没修。

    顺序：`SecurityGuard` 在构造时就 `Path(d).resolve()`
    （`core/security.py:116`），必须先 chdir、后构造。所以要把整个测试循环
    包在 `with` 里，而不是让各文件自己的 `_init_handler()` 去管。

    别把路径换成绝对 tmp：有些断言盯的就是 `workspace` 这个字面量
    （`parent.name == "workspace"`）—— 换个名字就把它们测的东西改掉了。
    "在 tmp 里造一个同名目录"两件事都满足。
    """
    old_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for d in dirs:
            (Path(tmp) / d).mkdir(parents=True, exist_ok=True)
        os.chdir(tmp)
        try:
            yield Path(tmp)
        finally:
            os.chdir(old_cwd)


#: SQLite 的伴生文件后缀—— 不进污染指纹。
#:
#: 判据：指纹要回答的是「这条测试改了什么」。这三个文件不是数据 ——
#: 它们是 SQLite 自己的共享结构，任何打开那个库的进程都会动它们的 mtime：
#:   * `-shm` —— WAL 的共享内存索引。只要有人打开库就会被碰：实测一个
#:     `mode=ro` 的 2 秒轮询监听器就足以让它每 2 秒变一次（当天当场误报了一次）。
#:   * `-wal` —— 已提交、未 checkpoint 的帧。它是一条共享通道：框架在跑
#:     的时候每写一轮它都长一截，那个变化归不了因（分不清是测试写的还是
#:     框架写的），而"归因"恰好是本指纹存在的全部理由。
#:   * `-journal` —— 回滚日志，只在一笔写事务期间存在。真写了的话 `.db` 本体
#:     会跟着变，不靠它抓。
#:排除的是「别人的动」，不是「测试的动」。
#:
#: 只排伴生文件，绝不顺手排整个 `data/memory/` —— 那会把 `short_term.db`
#: 本体一起放掉，而"某条测试真往生产库里写了"正是要抓的那种。
#: 这条判据不是偶发：框架一开着跑全量就中 —— 而那是开发时最常见的状态，
#: 于是它会训练人忽略红色。
#:
#: 唯一一份，两个读者共用：本模块的 `prod_snapshot` 与
#: `scripts/check_test_pollution.py::snapshot`（各写一份就会漂移）。
SQLITE_COMPANION_SUFFIXES = ("-shm", "-wal", "-journal")


def is_sqlite_companion(p) -> bool:
    """*p* 是不是 SQLite 的伴生文件（判据见 `SQLITE_COMPANION_SUFFIXES` 上方）。"""
    return str(p).endswith(SQLITE_COMPANION_SUFFIXES)


def tmp_audit_log(name: str = "lv_test_audit.log") -> str:
    """测试用的审计日志路径 —— 一律指向系统临时目录。

    这个仓库本来就有这个做法（`test_workspace_index` / `test_delete_approval_scope` /
    `test_approval_scope` 都写 `tempfile.gettempdir()`）。而
    `test_file_parser.py` 与 `test_office_parse.py` 写的是 `./logs/audit_test.log`
直接往生产 `logs/` 里写（实测 638 KB，每次全量测试都被改写，
    而"路径集合"式的机检看不出差别 —— 见 `prod_snapshot` 的 docstring）。
    """
    return str(Path(tempfile.gettempdir()) / name)


#: 除了 `DEFAULT_DIRS` 那几个目录之外，还要盯住的单个文件。
#: `config.yaml` 是生产配置，被
#: `web/server.py::_switch_provider` / `_switch_model` 逐行改写；
#: 它不在任何 `DEFAULT_DIRS` 里，改它不进指纹。
#: 往里加东西要先问"它是不是生产的东西" —— 测试夹具不许进这张表。
EXTRA_SNAPSHOT_FILES: tuple[str, ...] = ("config.yaml",)


def prod_snapshot(project_root: Path | None = None) -> dict:
    """生产目录的指纹：{相对路径: (大小, mtime_ns)}。

    判据是 `(路径, 大小, mtime)`，不是路径集合。
    第一版只比路径，于是它报了假"干净"：`data/memory/test_*.db`
    与 `logs/audit_test.log` 本来就存在、测试每跑一次改写一次，
    而路径集合看不出任何差别 —— 一边漏检、一边报"干净"。

    SQLite 的伴生文件不进指纹——
    判据见 `SQLITE_COMPANION_SUFFIXES` 上方那一整段（一句话：那些 mtime 是
    别人的动，把它算进来 = 把框架自己的写入当成本条测试的罪证）。
    """
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    out: dict = {}
    for d in DEFAULT_DIRS:
        base = root / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_dir() or "__pycache__" in p.parts:
                continue
            if is_sqlite_companion(p):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    # `config.yaml` 也要进指纹 ——
    #   它不在 `DEFAULT_DIRS` 里，于是"某条测试改了生产 `config.yaml`"
    #   这一类污染原来是完全看不见的。
    #   现场：`tests/test_model_configs_api.py` 第 5 节那条打了一个真的
    #   `/api/config {section: provider}`，`_switch_provider` 把生产
    #   `config.yaml` 的 `model.provider/model_name/base_url/api_key` 四行
    #   逐行改成了 qwen，而全绿。（当场用 `git diff` 抓到的。）
    #   任何写 `config.yaml` 的测试都该先被这条挡下来；读它不受影响
    #     （指纹只进 `out`，不改任何行为）。
    for extra in EXTRA_SNAPSHOT_FILES:
        p = root / extra
        if not p.exists():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out[extra] = (st.st_size, st.st_mtime_ns)
    return out


def prod_diff(before: dict, after: dict) -> dict:
    """{路径: 'added'|'removed'|'modified'}。"""
    changes = {}
    for k, v in after.items():
        if k not in before:
            changes[k] = "added"
        elif before[k] != v:
            changes[k] = "modified"
    for k in before:
        if k not in after:
            changes[k] = "removed"
    return changes


# ----------------------------------------------------------------------
# 模式：走工具派发的用例必须自己钉
# ----------------------------------------------------------------------

def neutralise_settings() -> None:
    """把 `configured_mode()` / `configured_level()` 的输入源指到一个不存在的文件。

    这是"用例不许继承开发机的设置"的正解（`pin_mode` 那条路从根上就脆）。

    ## 为什么 `pin_mode` 不够

    第一版给 6 个文件各钉一个固定 session 名单。变异验证（把开发机的
    `default_mode` 设成 `plan`）当场证明它漏：那些用例**每个都生成自己的
    session id**（`"probe:args:bad"` / `":empty:xx"` / `":audit"` …）
名单永远钉不全，而且以后新加的用例照样漏。

    ## 它做什么 / 不做什么

    它不是把函数换成替身 —— 它换的是输入源（`_SETTINGS_PATH` 指到
    一个不存在的路径），生产那三段代码一行没动，走的是它们本来就有的
    "读不到，默认"那一支（/ 那条判据）。
    同 `sandbox()` 的思路（chdir 换掉相对路径解析到哪儿），不是 mock。

    只影响本进程（测试进程），而且在模块 import 时调一次即可 ——
    它覆盖所有 session id，现在与将来。
    顺带把 `core/tiers` 的那一份也指走（同一个文件、同一类继承）。
    """
    import tempfile
    from pathlib import Path as _P

    void = _P(tempfile.gettempdir()) / "__lv_no_such_settings__.json"
    # `core.mail_config`：**它读的是同一份 `data/settings.json`，
    #   而里面躺着发件邮箱与授权码**。不指走的话，任何"忘了传凭据"的
    #   `send_email` 调用会拿开发机的真账号去连 SMTP（网络通就直接发出去）。
    #同一条规矩：用例不许继承开发机的设置。
    for mod_name in ("core.modes", "core.tiers", "core.mail_config"):
        try:
            mod = __import__(mod_name, fromlist=["_SETTINGS_PATH"])
            mod._SETTINGS_PATH = void
        except Exception as exc:  # noqa: BLE001
            print("  [!] neutralise_settings: %s 没换成：%s" % (mod_name, exc))
    try:
        from core import modes as M
        M.reset_mode_cache()          # 已经冻结的值也一并丢掉
    except Exception:  # noqa: BLE001
        pass


def pin_mode(*session_ids, key: str = "manual") -> None:
    """把这几个会话的交互模式钉成 *key*（默认「手动」）。

    ## 为什么需要它

    有了模式之后，它会改变派发：
      * `plan`，写工具被计划模式的闸拒掉（`is_allowed_in_plan_mode`）
      * `auto`，闸去调分类器（`_auto_review_gate`）—— 对一个用
        脚本化 fake provider 的用例来说，那就是"凭空多一次模型调用"，
        把脚本吃光，`FakeProvider ran out of scripted responses`

    而 `core.modes.configured_mode()` 读的是生产 `data/settings.json`。
开发机上那个键只要是 `auto`/`plan`，任何没钉模式的循环用例都会红
    —— 而红的样子（"脚本用完了"）完全不像模式问题。

    判据同本仓库那条「探针的输入源必须先核对生产从哪里读」：
    用例要么沙箱（`sandbox()` 里没有 settings.json，落回默认 `manual`），
    要么自己钉。别继承开发机的设置。

    实测：一次 `/mode auto` 之后，全量跑出 3 条红
    （`test_agent_loop_events` / `test_approval_replay` / `test_chunk_tools_guard`），
    而它们一个字都不该关心模式。
    """
    try:
        from core import modes as M
    except Exception:  # pragma: no cover
        return
    for sid in session_ids:
        try:
            M.set_mode(str(sid), key)
        except Exception as exc:  # noqa: BLE001
            print("  [!] pin_mode(%r, %r) 失败：%s" % (sid, key, exc))
