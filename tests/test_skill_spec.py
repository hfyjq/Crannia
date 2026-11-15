"""
SKILL_SPEC.md conformance —— manifest 校验、skill 装载、脚手架产物验证。

Usage:
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_skill_spec.py
"""
#@exclusive: skills-dir
# 这个文件仍然读真的 `skills/`（manifest / handler 存在性检查 + 一次 walk），
#   而同组的 `test_skill_doctor` / `test_skill_index` 也在读它，三个一起串行。
# 「建又删」的那一半已经沙箱化了（见下面两个 scaffold 用例）
#这一组不再有写者。标记先留着 —— 摘掉整组是另一次改动，而
#   有一条读者正钉着「这三个文件必须被认成 skills-dir」。
# → 机制与理由在 `scripts/run_tests.py::_EXCLUSIVE_RE`。

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# `_sandbox`：临时目录 + 跑完核对生产目录没被动
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sandbox import sandbox  # noqa: E402


def _purge_skills_modules() -> None:
    """把 `skills` 包及其子模块从 `sys.modules` 里摘掉。

    沙箱里那份 `skills` 会被 `importlib` 缓存住；不清的话下一次导入
    拿到的是上一个临时目录里的模块（而那个目录已经没了）。
    """
    for k in [m for m in sys.modules if m == "skills" or m.startswith("skills.")]:
        del sys.modules[k]


# ------------------------------------------------------------------
# Manifest validation tests (bus/loaders/skill_loader.validate_tool_schema)
# ------------------------------------------------------------------

def test_validate_valid_schema():
    """A valid JSON Schema 2020-12 passes validation."""
    from bus.loaders.skill_loader import validate_tool_schema

    valid = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
        },
        "required": ["query"],
    }
    # Should not raise
    validate_tool_schema(valid)


def test_validate_missing_type():
    """Schema without type='object' raises ValueError."""
    from bus.loaders.skill_loader import validate_tool_schema

    invalid = {
        "type": "string",  # wrong root type
        "properties": {},
    }
    try:
        validate_tool_schema(invalid)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "object" in str(e)


def test_validate_bad_property_type():
    """Property with unrecognized type raises ValueError."""
    from bus.loaders.skill_loader import validate_tool_schema

    invalid = {
        "type": "object",
        "properties": {
            "bad_prop": {
                "type": "unknown_type",
                "description": "Invalid",
            },
        },
    }
    try:
        validate_tool_schema(invalid)
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_validate_required_not_in_properties():
    """Required field not in properties raises ValueError."""
    from bus.loaders.skill_loader import validate_tool_schema

    invalid = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Name"},
        },
        "required": ["missing_field"],
    }
    try:
        validate_tool_schema(invalid)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "missing_field" in str(e)


def test_validate_not_a_dict():
    """Non-dict schema raises ValueError."""
    from bus.loaders.skill_loader import validate_tool_schema

    try:
        validate_tool_schema([])  # list, not dict
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_validate_bad_schema_uri():
    """Unrecognized $schema URI raises ValueError."""
    from bus.loaders.skill_loader import validate_tool_schema

    invalid = {
        "$schema": "http://example.com/fake-schema",
        "type": "object",
        "properties": {},
    }
    try:
        validate_tool_schema(invalid)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "$schema" in str(e)


# ------------------------------------------------------------------
# SKILL.md parsing tests (bus/loaders/skill_loader._parse_skill_md)
# ------------------------------------------------------------------

def test_parse_skill_md_valid():
    """A valid SKILL.md is parsed correctly."""
    from bus.loaders.skill_loader import _parse_skill_md

    skill_dir = Path("skills/file_parser")
    result = _parse_skill_md(skill_dir)
    assert result is not None, "Should parse file_parser SKILL.md"
    assert result["name"] == "file-parser"
    assert len(result["description"]) > 0
    assert len(result["body"]) > 0
    assert result["raw_metadata"]["metadata"]["kind"] == "skill"


def test_parse_skill_md_entertainment():
    """An existing built-in SKILL.md parses correctly."""
    from bus.loaders.skill_loader import _parse_skill_md

    skill_dir = Path("skills/entertainment")
    result = _parse_skill_md(skill_dir)
    assert result is not None, "entertainment SKILL.md should exist and parse"
    assert result["name"] == "entertainment"
    assert len(result["description"]) > 0


def test_parse_skill_md_office_parser():
    """office_parser SKILL.md parses with updated tools list."""
    from bus.loaders.skill_loader import _parse_skill_md

    skill_dir = Path("skills/office_parser")
    result = _parse_skill_md(skill_dir)
    assert result is not None
    # Verify write tools are listed
    tools_str = result["raw_metadata"]["metadata"]["tools"]
    assert "create_document" in tools_str
    assert "extract_document_structure" in tools_str
    assert "export_document" in tools_str


def test_parse_skill_md_nonexistent():
    """Returns None for directory without SKILL.md."""
    from bus.loaders.skill_loader import _parse_skill_md

    # skills/__init__.py is a file, not a directory with SKILL.md
    result = _parse_skill_md(Path("tests"))
    assert result is None


# ------------------------------------------------------------------
# Scaffold tests
# ------------------------------------------------------------------

def test_the_default_skills_dir_is_still_the_real_one():
    """反面：不传 `skills_dir` 时，落的仍然是生产那个 `skills/`。

    没有这条，把默认值改坏（比如顺手改成 `Path.cwd()/"skills"`）不会有任何东西红 ——
    这是一个"看起来对"的默认值。
    """
    from scripts.create_skill import _SKILLS_DIR
    assert _SKILLS_DIR == _PROJECT_ROOT / "skills", _SKILLS_DIR


def test_scaffold_creates_minimal_skill():
    """Scaffold creates a directory with required files.

    它跑在临时目录里。直接在真 `skills/` 上建又删时，净变化为零
端到端污染自查（比前后快照）看不见 —— 并行跑起来还会撞进别人的
    观察窗口，变成假红。
    """
    from scripts.create_skill import create_skill

    with sandbox() as tmp:
        skills_dir = tmp / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        target = skills_dir / "test_scaffold_min"

        exit_code = create_skill(
            "test_scaffold_min",
            "A minimal test skill",
            force=False,
            skills_dir=skills_dir,
        )
        assert exit_code == 0

        # Required files exist
        assert (target / "__init__.py").exists()
        assert (target / "manifest.yaml").exists()
        assert (target / "handler.py").exists()
        assert (target / "SKILL.md").exists()

        # manifest.yaml has correct kind
        import yaml
        with open(target / "manifest.yaml", "r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        assert manifest["kind"] == "skill"
        assert manifest["name"] == "test_scaffold_min"


def test_scaffold_handler_runs():
    """Generated handler imports and executes correctly after init.

    "导入"这一步就是这条为什么"不是一行" —— 生成的模块靠
    `sys.path` 上的包根才能 `import skills.<name>.handler`，所以临时目录里
    要造一个 `skills/__init__.py`，并把包根插到 `sys.path` 最前面。
    """
    import importlib

    from scripts.create_skill import create_skill

    with sandbox() as tmp:
        skills_dir = tmp / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "__init__.py").write_text("", encoding="utf-8")
        target = skills_dir / "test_scaffold_run"

        exit_code = create_skill(
            "test_scaffold_run",
            "Scaffold runtime test",
            force=False,
            skills_dir=skills_dir,
        )
        assert exit_code == 0

        # Import and run the generated handler
        from core.security import SecurityGuard
        guard = SecurityGuard(["./workspace"], [], "./logs/test.log")

        sys.path.insert(0, str(tmp))       # 包根 → `skills` 解析到临时目录
        _purge_skills_modules()
        try:
            mod = importlib.import_module("skills.test_scaffold_run.handler")
            # 判据是"导进来的确实是沙箱那一份"，不是"导入没抛"
            assert Path(mod.__file__).resolve().is_relative_to(tmp.resolve()), \
                mod.__file__
            mod.init(guard)

            # Find the first tool handler and call it
            tool_handler = getattr(mod, "test_scaffold_run_action")
            result = tool_handler("hello")
            assert result["success"] is True
            assert "message" in result
        finally:
            _purge_skills_modules()
            try:
                sys.path.remove(str(tmp))
            except ValueError:
                pass

        # Verify __init__.py is empty (matches native file_parser/office_parser)
        assert (target / "__init__.py").stat().st_size == 0


def _duplicate_keys(text: str):
    """`text` 里所有重复的映射键，返回 `[(路径, 键)]`。

    不能用 `yaml.safe_load` —— 它对重复键静默：后者直接覆盖前者，
    连个警告都没有。`yaml.compose` 保留全部键值对，只有在节点层才看得见重复。
    """
    import yaml

    def walk(node, path=""):
        out = []
        if isinstance(node, yaml.MappingNode):
            seen = set()
            for k, v in node.value:
                key = getattr(k, "value", None)
                if key in seen:
                    out.append((path, key))
                seen.add(key)
                out += walk(v, path + "/" + str(key))
        elif isinstance(node, yaml.SequenceNode):
            for i, v in enumerate(node.value):
                out += walk(v, path + "[%d]" % i)
        return out

    return walk(yaml.compose(text))


def test_no_manifest_has_duplicate_mapping_keys():
    """manifest 里不许有重复的映射键。

    ## 典型形状

    一个路径形状的 `pattern`（`workspace/_测试区C/*`）被报「工作区中没有
    匹配的文件」，而真正的原因是：

    ```yaml
    pattern:
      description: "Glob pattern to bulk-delete … e.g. '*wip*' …"     # ← 这句
      description: "List of absolute or workspace-relative file paths to delete"   # ← 被它盖掉
    ```

模型实际收到的说明属于 `file_paths`，它照着"一串路径"去写 pattern。
    它没做错，是我们喂错了说明。

    ## 为什么必须有这条读者

    YAML 的重复键完全静默：解析成功、装载成功、跑得通，而行为是坏的。
    「没有读者的清单会烂」在这里是"没有读者的静默覆盖会烂"。

    配一条自造靶子：给检测器喂一段故意重复的 YAML，
    它必须挑得出来 —— 否则这条断言今天能过只是因为恰好没有重复
    （判别力寄存于未来）。
    """
    from pathlib import Path as _P

    # ① 自造靶子：检测器自己得能红
    bad = ("a:\n  b: 1\n  b: 2\n"
           "tools:\n  - name: x\n    p:\n      d: 1\n      d: 2\n")
    hits = _duplicate_keys(bad)
    assert sorted(k for _, k in hits) == ["b", "d"], (
        "检测器自己坏了 —— 它对故意重复的 YAML 只报出 %r" % (hits,))
    assert _duplicate_keys("a:\n  b: 1\nc: 2\n") == [], "它对干净的 YAML 报了重复"

    # ② 真的去扫
    roots = [_P("skills"), _P("providers")]
    files = []
    for r in roots:
        if r.is_dir():
            files += sorted(r.glob("*/manifest.yaml"))
    assert len(files) >= 10, (
        "只找到 %d 个 manifest —— 扫描面太窄，这条断言会空跑" % len(files))

    offenders = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for path, key in _duplicate_keys(text):
            offenders.append("%s: %s/%s" % (p.as_posix(), path, key))
    assert not offenders, (
        "这些 manifest 里有**重复的映射键**（YAML 静默取最后一个 -> "
        "作者以为生效的那句被盖掉了）：\n  " + "\n  ".join(offenders))


def test_scaffold_rejects_invalid_name():
    """Scaffold returns error for invalid skill name."""
    from scripts.create_skill import create_skill

    exit_code = create_skill("123bad", "Invalid name starting with digit")
    assert exit_code != 0


# ------------------------------------------------------------------
def test_skill_imports_point_at_existing_skills():
    """skill 之间互相 import 的目标必须真的存在。

    判据是目录存在，不是"import 得通" —— 后者要执行 handler，
    而其中一半的依赖是可选装的。这条只问一句：这个名字底下还有东西吗。

    加它的理由是一次真事故：一个 skill 被整体删除之后，另一个 skill 里
    `from skills.<被删的>.handler import ...` 仍然活着，被 `except Exception`
    兜成一句 "unavailable"，于是那个工具每次都失败而没有任何东西红。
    """
    import ast
    skills_dir = _PROJECT_ROOT / "skills"
    assert skills_dir.is_dir(), "setup: 找不到 skills/"

    offenders = []
    for path in sorted(skills_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            offenders.append("%s: 读不动/解析失败（%s）" % (path, exc))
            continue
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "skills":
                    names.extend(a.name for a in node.names)
                elif node.module.startswith("skills."):
                    names.append(node.module.split(".")[1])
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("skills."):
                        names.append(a.name.split(".")[1])
        for name in names:
            if name in ("__init__", ""):
                continue
            if not (skills_dir / name).is_dir():
                offenders.append(
                    "%s -> skills.%s（目录不存在）" % (
                        path.relative_to(_PROJECT_ROOT).as_posix(), name))

    assert not offenders, (
        "cross-skill imports point at skills that no longer exist:\n  "
        + "\n  ".join(offenders)
        + "\n=> remove references when deleting a skill; `except Exception`"
          " swallows this whole path as 'unavailable' and nothing goes red."
    )
    print("  [ok] cross-skill imports all point at existing dirs")


# Runner
# ------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        # Schema validation
        ("test_validate_valid_schema", test_validate_valid_schema),
        ("test_validate_missing_type", test_validate_missing_type),
        ("test_validate_bad_property_type", test_validate_bad_property_type),
        ("test_validate_required_not_in_properties", test_validate_required_not_in_properties),
        ("test_validate_not_a_dict", test_validate_not_a_dict),
        ("test_validate_bad_schema_uri", test_validate_bad_schema_uri),
        # SKILL.md parsing
        ("test_parse_skill_md_valid", test_parse_skill_md_valid),
        ("test_parse_skill_md_entertainment", test_parse_skill_md_entertainment),
        ("test_parse_skill_md_office_parser", test_parse_skill_md_office_parser),
        ("test_parse_skill_md_nonexistent", test_parse_skill_md_nonexistent),
        # Scaffold
        ("test_the_default_skills_dir_is_still_the_real_one",
         test_the_default_skills_dir_is_still_the_real_one),
        ("test_scaffold_creates_minimal_skill", test_scaffold_creates_minimal_skill),
        ("test_scaffold_handler_runs", test_scaffold_handler_runs),
        ("test_no_manifest_has_duplicate_mapping_keys",
         test_no_manifest_has_duplicate_mapping_keys),
        ("test_scaffold_rejects_invalid_name", test_scaffold_rejects_invalid_name),
        ("test_skill_imports_point_at_existing_skills",
         test_skill_imports_point_at_existing_skills),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")

    print(f"\n{len(tests)} tests: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
