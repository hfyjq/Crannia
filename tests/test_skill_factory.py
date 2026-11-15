"""meta-skill —— `create_skill`（`core/skill_factory.py`）机械读者。

## 两条不变量的读者

```
① 拒绝已存在的名字      —— 模型不许改现成的 skill
② 强制 draft: true      —— 模型设不了这个字段
```

两条都各配一条负对照（"模型给 `draft: false` 也必须是 `true`" /
"重名时目录一个字节不许动"），否则它们可能只是"碰巧没被触发"。

## 一条最容易写成恒真的断言

「体检不过，一个字节都没落盘」 —— 光断言 `success is False` 是不够的：
把"先写进去再删掉"写成"先写进去然后忘了删"，`success` 照样是 False。
判据必须是 `skills/` 下新增了哪些条目（逐条比），而不是返回值。

## 第三件事：它不能偷偷改运行态

`create_skill` 只产草稿，它跑完之后注册表里不许多出任何工具
（`draft` 的 skill 不会被装载）。这条同时钉住了"草稿真的不生效"。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_skill_factory.py
"""

import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_MANIFEST = """kind: skill
name: demo
draft: false
load_strategy: on_demand
description: a demo skill
tools:
  - name: demo_tool
    permission: write
    destructive: false
    description: does a thing
    inputSchema:
      type: object
      properties:
        text:
          type: string
      required:
        - text
"""

_HANDLER = """\

def init(security_guard=None):
    pass


def demo_tool(text=""):
    return {"success": True, "echo": text}
"""


def _manifest_for(name: str) -> str:
    """把样板 manifest 改成 *name* 的 —— 只换那一处（`str.replace` 默认全替换，
    `name: demo` 会连 `name: demo_tool` 一起吃掉）。"""
    return (_MANIFEST.replace("name: demo\n", "name: %s\n" % name, 1)
            .replace("demo_tool", "%s_tool" % name))


def _handler_for(name: str) -> str:
    return _HANDLER.replace("demo_tool", "%s_tool" % name)


class _Lab:
    """在临时目录里假装一个框架工作目录（`skills/` + 一个真的注册表）。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._cwd = os.getcwd()
        os.chdir(self._tmp.name)
        Path("skills").mkdir()
        return self

    def __exit__(self, *exc):
        os.chdir(self._cwd)
        self._tmp.cleanup()
        return False

    def skills(self):
        return sorted(p.name for p in Path("skills").iterdir())


# 1. 正常那条路 + 不变量 ②（强制 draft）

def test_creates_a_draft_and_forces_the_three_fields():
    """模型给 `draft: false` 也必须变成 `true` —— 这是不变量 ②。

    没有这条，模型（或一次提示注入）只要在 YAML 里写 `draft: false`，
    造出来的东西就当场生效了 —— 而它是模型写的 Python。
    """
    from core.skill_factory import create_skill

    with _Lab() as lab:
        assert "draft: false" in _MANIFEST, "夹具里得先有那个 false，不然这条测不到"
        r = create_skill(name="demo", manifest_yaml=_MANIFEST, handler_py=_HANDLER)
        assert r.get("success") is True, r
        assert lab.skills() == ["demo"], lab.skills()

        text = Path("skills/demo/manifest.yaml").read_text(encoding="utf-8")
        assert "draft: true" in text, (
            "★ 模型给的 draft: false 没被强制成 true —— 不变量 ② 破了：\n%s" % text)
        assert "draft: false" not in text
        assert "kind: skill" in text
        assert "name: demo" in text
        assert r.get("forced"), "改了字段却没说 —— 静默改口径：%r" % (r,)
        assert r.get("draft") is True
        assert Path("skills/demo/handler.py").is_file()
    print("  [ok] 造出草稿，且 `draft: false` 被强制成 `true`（并说出了改了什么）")


#: 一个读家族的样板 —— 不变量 ③ 靠它才测得到。
#: 原夹具那个 `demo_tool` 不落读家族（名字不以 get_/read_/... 开头）
#:用它测不变量 ③ 是恒真的（永远走不到那条判据）。
_READ_MANIFEST = """kind: skill
name: demo
draft: false
load_strategy: on_demand
description: a demo skill
tools:
  - name: get_demo_weather
    permission: read
    destructive: false
    %s
    description: 查天气
    inputSchema:
      type: object
      properties:
        city:
          type: string
"""


def test_refuses_a_read_family_tool_without_output_kind():
    """不变量 ③：读家族工具没声明形态，拒。

    前两条不变量（拒绝重名 / 强制 draft）撑的是「模型写的不会自己生效」。
    这一条撑的是另一件事：模型造的工具必须能被隔离面分类。

    背景一句话：隔离面原本是 `core/agent_loop.py` 的硬编码名单，
    前提是"谁加工具谁去那儿登记" —— 而造包的人（含模型）
    没有那个权限，也不该有。决定权搬进 manifest，这里是守门人。

    判据是 fail-closed：没声明不是「默认安全」，是「没做决定」。
    """
    from core.skill_factory import create_skill

    with _Lab() as lab:
        # ① 没声明，拒，且一个字节都不许落盘
        r = create_skill(name="demo", manifest_yaml=_READ_MANIFEST % "",
                         handler_py=_HANDLER)
        assert r.get("success") is not True, (
            "读家族工具没声明 output_kind 却造出来了：%r" % (r,))
        assert "output_kind" in str(r.get("error", "")) + str(r.get("hint", "")), (
            "拒了但没说清要补什么：%r" % (r,))
        assert lab.skills() == [], (
            "被拒了却落了盘 —— 那是「拒」这条承诺破了：%r" % (lab.skills(),))

        # ② 非法值，同样拒（拼错 `structred` 不许被当成"声明过了"）
        r2 = create_skill(name="demo", manifest_yaml=_READ_MANIFEST % "output_kind: structred",
                          handler_py=_HANDLER)
        assert r2.get("success") is not True, "拼错的 output_kind 被放行了：%r" % (r2,)

        # ③ 声明对了，放行（否则上面两条是空转：谁都过不去）
        r3 = create_skill(name="demo", manifest_yaml=_READ_MANIFEST % "output_kind: structured",
                          handler_py=_HANDLER.replace("demo_tool", "get_demo_weather"))
        assert r3.get("success") is True, r3
        assert lab.skills() == ["demo"], lab.skills()
        text = Path("skills/demo/manifest.yaml").read_text(encoding="utf-8")
        assert "output_kind: structured" in text, text

        # ④ 非读家族的工具不要求这个字段（否则这条规矩会变成全仓的税）
        r4 = create_skill(name="plain", manifest_yaml=_manifest_for("plain"),
                          handler_py=_handler_for("plain"))
        assert r4.get("success") is True, (
            "`demo_tool` 不落读家族，不该被要求声明：%r" % (r4,))
    print("  [ok] 读家族没声明 -> 拒（非法值也拒）· 声明对了才放行 · 非读家族不要求")


def test_refuses_a_tool_without_permission():
    """不变量④：每个工具都要表态它能不能改东西。

    它比 ③ 更宽（③ 只管读家族），理由是代价方向：`read` 是唯一有闸含义的
    那一档，一个名字像 `get_` / `list_` / `parse_` 的写工具会被前缀猜成 `read`
并发跑 + 只读子 agent 放行它，而不会有任何东西红。
    """
    from core.skill_factory import create_skill

    with _Lab() as lab:
        # ① 缺，拒，且一个字节都不许落盘
        r = create_skill(name="noperm",
                         manifest_yaml=_MANIFEST.replace("    permission: write\n", "", 1),
                         handler_py=_HANDLER)
        assert r.get("success") is not True, (
            "没声明 permission 却造出来了：%r" % (r,))
        # 这一条断言是"故意分层"的 —— 它要的是工厂自己那条话术。
        # 不变量④ 若被关掉，体检器那一层仍然拦得住
        # （它报 `tool_permission_missing` `SEV_FAIL`，`create_skill` 走体检那条路拒），
        # 于是 `success` 还是 False —— 只有"断言里必须出现 permission"这一条会红。
        #若把断言放宽成"只要被拒就行"，去掉不变量④ 就再也不会有人发现，
        #    而两层纵深会静默塌成一层。
        assert "permission" in str(r.get("error", "")) + str(r.get("hint", "")), (
            "拒了但没说清要补什么（或者：拦住它的是体检器那一层，不是工厂的"
            "不变量④ —— 两层里掉了一层）：%r" % (r,))
        assert lab.skills() == [], (
            "被拒了却落了盘 —— 那是「拒」这条承诺破了：%r" % (lab.skills(),))

        # ② 非法值，同样拒（拼错不许被当成"声明过了"）
        r2 = create_skill(name="badperm",
                          manifest_yaml=_MANIFEST.replace("permission: write",
                                                          "permission: Write", 1),
                          handler_py=_HANDLER)
        assert r2.get("success") is not True, "拼错的 permission 被放行了：%r" % (r2,)

        # ③ 写对了，放行（否则上面两条是空转：谁都过不去）
        r3 = create_skill(name="okperm", manifest_yaml=_MANIFEST, handler_py=_HANDLER)
        assert r3.get("success") is True, r3
        assert lab.skills() == ["okperm"], lab.skills()
    print("  [ok] 缺 permission -> 拒（不落盘）· 拼错也拒 · 写对了才放行")


def test_refuses_a_tool_without_destructive():
    """不变量 ⑤：每个工具都要声明 `destructive`。

    这一条比 `permission` 那条更要紧，理由在代价方向：
    `permission` 没声明兜到 `admin`（fail-closed，安全侧）；
    `destructive` 没声明兜到 `false`（fail-open），一个没人看过的工具
    会静默地不算不可逆，兜底闸连问都不会问。
承重的只有三道：AST 反查 · 棘轮 · 这里。
    与上面那条同一条纪律：断言要的是工厂自己那条话术 ——
    只断言"被拒"的话，关掉不变量⑤之后体检器那一层仍然拦得住，
    而两层纵深会静默塌成一层（上面那条的注释里记过这个形状）。
    """
    from core.skill_factory import create_skill

    with _Lab() as lab:
        # ① 缺，拒，且一个字节都不许落盘
        r = create_skill(name="nodest",
                         manifest_yaml=_MANIFEST.replace(
                             "    destructive: false\n", "", 1),
                         handler_py=_HANDLER)
        assert r.get("success") is not True, (
            "没声明 destructive 却造出来了：%r" % (r,))
        assert "destructive" in str(r.get("error", "")) + str(r.get("hint", "")), (
            "拒了但没说清要补什么（或者：拦住它的是体检器那一层，不是工厂的"
            "不变量⑤ —— 两层里掉了一层）：%r" % (r,))
        assert lab.skills() == [], (
            "被拒了却落了盘 —— 那是「拒」这条承诺破了：%r" % (lab.skills(),))

        # ② 非布尔，同样拒 —— `"true"`（带引号，字符串）、`1`（整数）都不算。
        #    这一条尤其要紧：这一根的失败方向是 fail-open，
        #    一个"看起来写了"的值被收下等于假装做过决定。
        # `yes` 不在这个名单里 —— PyYAML 把它读成 `True`（是布尔），
        #    所以它会被收下。那是"碰巧对"，不是保证；判据仍是 `isinstance(v, bool)`。
        # 名字每条不同：同名会在撞名那一关就被拒 ——
        #    那样断言照样过，但测的不是这件事（"绿了但没测到"）。
        for i, bad in enumerate(("\"true\"", "1")):
            r2 = create_skill(name="baddest%d" % i,
                              manifest_yaml=_MANIFEST.replace(
                                  "destructive: false", "destructive: %s" % bad, 1),
                              handler_py=_HANDLER)
            assert r2.get("success") is not True, (
                "非布尔的 destructive=%s 被放行了：%r" % (bad, r2,))

        # ③ 写对了，放行（否则上面两条是空转：谁都过不去）
        r3 = create_skill(name="okdest", manifest_yaml=_MANIFEST, handler_py=_HANDLER)
        assert r3.get("success") is True, r3
        assert lab.skills() == ["okdest"], lab.skills()
    print("  [ok] 缺 destructive -> 拒（不落盘）· 非布尔也拒 · 写对了才放行")


def test_creating_does_not_touch_the_live_registry():
    """它只产草稿，注册表里不许多出任何工具（`draft` 的 skill 不被装载）。

    这条同时是"草稿真的不生效"的读者 —— 而那正是整个安全论证的支点。
    """
    from core.skill_factory import create_skill

    with _Lab():
        import sys as _sys
        if str(_PROJECT_ROOT / "scripts") not in _sys.path:
            _sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
        from measure_request import boot_registry
        import bus.registry as R

        _cwd = os.getcwd()
        os.chdir(_PROJECT_ROOT)          # boot 必须在真目录里（它扫 skills/）
        boot_registry()
        os.chdir(_cwd)

        before = set(R.list_tools())
        r = create_skill(name="demo", manifest_yaml=_MANIFEST, handler_py=_HANDLER)
        assert r.get("success") is True, r
        after = set(R.list_tools())
        assert after == before, (
            "★ 造了个草稿，注册表却变了 —— 那不是草稿：%r" % (after - before,))
    print("  [ok] 造草稿不动注册表（草稿真的不生效）")


# 2. 不变量 ①（不改现成的）+ 各种拒绝

def test_refuses_an_existing_name_and_leaves_it_untouched():
    """不变量 ①：不许改现成的 skill。

    判据不能只看 `success is False` —— 还要看那个目录一个字节没动。
    """
    from core.skill_factory import create_skill

    with _Lab():
        assert create_skill(name="demo", manifest_yaml=_MANIFEST,
                            handler_py=_HANDLER).get("success") is True
        marker = Path("skills/demo/handler.py").read_bytes()
        assert b"MARKER" not in marker

        r = create_skill(name="demo",
                         manifest_yaml=_MANIFEST.replace("a demo skill", "MARKER"),
                         handler_py="# MARKER\n" + _HANDLER)
        assert r.get("success") is False, r
        assert "已经存在" in r.get("error", ""), r
        assert Path("skills/demo/handler.py").read_bytes() == marker, (
            "★ 被拒了但**文件被改了** —— 模型改到了一个现成的 skill")
        assert len(list(Path("skills").iterdir())) == 1, "多了个目录"
    print("  [ok] 重名 -> 拒，且那个目录一个字节没动")


def test_refuses_bad_names():
    from core.skill_factory import create_skill

    with _Lab():
        for bad in ("", "Demo", "9demo", "de", "a" * 41, "de mo", "demo-x",
                    "skills", "__pycache__"):
            r = create_skill(name=bad, manifest_yaml=_MANIFEST, handler_py=_HANDLER)
            assert r.get("success") is False, "%r 居然通过了：%r" % (bad, r)
        assert list(Path("skills").iterdir()) == [], "坏名字也落了盘"
    print("  [ok] 8 种坏名字全拒（含 `skills` / `__pycache__` 这种保留名）")


def test_refuses_oversized_payloads():
    from core.skill_factory import _MAX_FILE_BYTES, _MAX_TOOLS, create_skill

    with _Lab():
        r = create_skill(name="big", manifest_yaml=_MANIFEST,
                         handler_py="x = 1\n" * (_MAX_FILE_BYTES // 3))
        assert r.get("success") is False, r
        assert "上限" in r.get("error", ""), r

        many = _MANIFEST.replace("  - name: demo_tool",
                                 "".join("  - name: t%d\n" % i
                                         for i in range(_MAX_TOOLS + 1)), 1)
        r = create_skill(name="many", manifest_yaml=many, handler_py=_HANDLER)
        assert r.get("success") is False, r
        assert list(Path("skills").iterdir()) == [], "被拒的还落了盘"
    print("  [ok] 超大文件 / 超多工具 都拒，且都没落盘")


def test_refuses_broken_yaml_and_missing_fields():
    from core.skill_factory import create_skill

    with _Lab():
        for label, mf, hp in (
            ("YAML 坏", "kind: [skill\n", _HANDLER),
            ("没有 tools", "kind: skill\nname: demo\n", _HANDLER),
            ("顶层不是映射", "- a\n- b\n", _HANDLER),
            ("没给 handler", _MANIFEST, ""),
        ):
            r = create_skill(name="x_%d" % len(label), manifest_yaml=mf, handler_py=hp)
            assert r.get("success") is False, "%s 居然通过了：%r" % (label, r)
        assert list(Path("skills").iterdir()) == [], "被拒的还落了盘"
    print("  [ok] 坏 YAML / 空 tools / 非映射 / 空 handler 都拒")


# 3. 先体检、后落盘（这条最容易写成恒真）

def test_a_failing_doctor_leaves_nothing_on_disk():
    """体检不过，`skills/` 下一个字节都没多。

    这条断言光看 `success is False` 是不够的：把实现写成
    "先落盘、再体检、忘了删"，`success` 照样是 False。
判据是目录里新增了哪些条目（`_wip_` 临时目录也不许残留 ——
      它就是"实现真的走了临时目录"的证据）。
    """
    from core.skill_factory import create_skill

    with _Lab():
        before = sorted(Path("skills").iterdir())
        for label, mf, hp in (
            ("语法错", _manifest_for("syn"), "def syn_tool(:\n"),
            ("声明的工具不存在", _manifest_for("ghost"), "def other():\n    pass\n"),
            ("inputSchema 非法",
             _manifest_for("bad_schema").replace("type: object", "type: array", 1),
             _handler_for("bad_schema")),
        ):
            r = create_skill(name=mf.split("name: ")[1].split("\n")[0],
                             manifest_yaml=mf, handler_py=hp)
            assert r.get("success") is False, "%s 居然通过了：%r" % (label, r)
            assert "体检" in r.get("error", ""), r
            assert r.get("report"), "没把体检报告带回来 —— 模型不知道改哪儿"

        after = sorted(Path("skills").iterdir())
        assert after == before, (
            "★ 体检不过却留下了东西（含 `_wip_` 临时目录）：%r"
            % ([p.name for p in set(after) - set(before)],))

        # 反面自证：好包必须真的落盘（不然上面那条可能是"什么都没发生"）
        assert create_skill(name="good", manifest_yaml=_manifest_for("good"),
                            handler_py=_handler_for("good")).get("success") is True
        assert len(list(Path("skills").iterdir())) == 1
    print("  [ok] 体检不过 -> 一个字节没落盘（含 `_wip_` 不残留）；好包照落")


def test_the_report_carries_the_users_copy_when_it_fails():
    """失败时回的是用户给的那段稿（`ui=False` 形态：徽章换 ASCII）。

    带符号那份不能出现在这里 —— 工具回执会进日志/CLI，
    而 `U+274C` 在 GBK 控制台上会崩（基线 60 不许增长）。
    """
    from core.skill_doctor import _UI_BADGE
    from core.skill_factory import create_skill

    with _Lab():
        r = create_skill(name="ghost", manifest_yaml=_manifest_for("ghost"),
                         handler_py="def other():\n    pass\n")
        rep = r.get("report", "")
        assert "错误插件：ghost" in rep, rep
        assert "对此结果有反馈？" in rep, rep
        assert _UI_BADGE not in rep, (
            "工具回执里带了会崩的符号：%r" % rep[:40])
    print("  [ok] 失败回执是用户那段稿，且**不带符号**")


# 4. 接线：常驻 / 权限 / 活动行

def test_it_is_registered_core_and_write_tier():
    import bus.registry as R

    _cwd = os.getcwd()
    os.chdir(_PROJECT_ROOT)
    try:
        if str(_PROJECT_ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
        from measure_request import boot_registry
        boot_registry()
    finally:
        os.chdir(_cwd)

    assert "create_skill" in R._CORE_TOOLS, (
        "不常驻 —— 模型不知道它存在就没法搜它，而造 skill 是用户点名要的那条路")
    assert R.get_tool_permission("create_skill") == "write", (
        "`create_` 前缀该命中 write：它写文件，不能进只读预取批次")

    for rel, needle in (("bus/core.py", "register_skill_factory_tool()"),
                        ("scripts/measure_request.py", "R.register_skill_factory_tool()")):
        src = (_PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert needle in src, "%s 里没有 %s —— 这条能力不存在" % (rel, needle)
    print("  [ok] 常驻 + write 档 + 两处接线都在")


def test_activity_line_does_not_fall_back():
    from core.activity import describe_tool

    line = describe_tool("create_skill")
    assert "正在" in line and "技能" in line, line
    print("  [ok] 活动行：%s" % line)


def _main():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS  " + name)
        except AssertionError as exc:
            failed += 1
            print("FAIL  " + name + ": " + str(exc))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print("ERROR " + name + ": " + type(exc).__name__ + ": " + str(exc))

    print("")
    print("Results: %d passed, %d failed" % (len(tests) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
