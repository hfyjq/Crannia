"""从 manifest.yaml 装载 provider：实例化 client、校验它实现了
BaseProvider，然后注册。
"""

import importlib
from pathlib import Path

import yaml

from bus.registry import register_provider
from core import window_estimate
from providers.base import BaseProvider


def _capability(model_cfg: dict, key: str, modality: str, default: bool,
                provider_dir: str = "", model_name: str = "") -> tuple[bool, str]:
    """`(值, 来源)` —— 声明 > 查表 > 兜底（顺序承重）。

    | 层 | 来源字符串 | 谁写的 |
    |---|---|---|
    | ① 显式键（`config.yaml` / 端配置条目的三态） | `声明(config.yaml)` / `声明(本配置)` | 用户 |
    | ② `providers/model_capabilities.json` | `查表(models.dev …)` | 上游 |
    | ③ 兜底 | `默认` | 我们（vision True / audio False） |

    没有 manifest 那一层：它是逐供应商的，
      而能力是逐模型的事实（见 `window_estimate` 的四层说明）。
    能力表的作用：端配置页换模型时不一定带能力键，
      没有这一层的话 `supports_vision` 永远是默认 True、`supports_audio` 永远 False
      —— 与实际选的模型脱钩。
    `provider_dir` / `model_name` 为空（测试、老调用），不查表，走兜底。
    显式值必须真的是 `bool`：`"false"` 是字符串，`bool("false")` 是 `True`
坏类型当"不支持"，并出声。
    """
    #: 来源标签跟着值走 —— 端配置条目的三态覆盖
    #:   与 `config.yaml` 的显式键走同一条判据，但用户是在两个地方设的，
    #:   界面上必须分得开。标签由产者放进来（`_model_cfg_of`）。
    label = "声明(config.yaml)"
    if isinstance(model_cfg, dict) and model_cfg.get("capability_source"):
        label = str(model_cfg["capability_source"])
    raw = model_cfg.get(key) if isinstance(model_cfg, dict) else None
    if raw is not None:
        if isinstance(raw, bool):
            return raw, label
        print(f"[bus] {label} 的 {key}={raw!r} 不是布尔"
              f"（{type(raw).__name__}）—— 按**不支持**处理"
              f"（设置写错了，先改对；`true`/`false` 不要加引号）")
        return False, label + "（坏类型）"
    if provider_dir and model_name:
        try:
            from core import model_capabilities
            hit = model_capabilities.supports(provider_dir, model_name, modality)
            if hit is True:
                # 表的肯定结论承重：它能把"默认关"的能力打开（音频）。
                return True, model_capabilities.source_note()
            if hit is False and default is False:
                # 与默认一致 —— 照样记来源（"表也说没有"）。
                return False, model_capabilities.source_note()
            if hit is False and default is True:
                # 表的否定结论不承重。
                #
                # 反例：models.dev 的快照把 `deepseek-v4-flash` 记成
                # `input: ["text"]`，而它能读图（贴带代码的截图，
                # 它逐字读出并做了 code review）。
                #表是外部维护的、会错的；而"否定"这一侧的代价是
                # 把一个能用的功能默认关掉（`accepts_images` docstring
                # 里那条判据：「默认关的实际效果是这个功能默认就是坏的」）。
                # 反方向的错（表说能、其实不能）是可诊断的：上游 400 +
                # `image_hint_for_failure` 那一行 + 用户可写的 `supports_vision: false`。
                #结论：表可以开，不可以关（除非用户自己关）。
                return default, "默认（表说没有，但默认仍开）"
        except Exception as exc:                                # noqa: BLE001
            print(f"[bus] 能力表查询失败（{type(exc).__name__}: {exc}）"
                  f" —— 落回默认")
    return default, "默认"


def _resolve_vision(model_cfg: dict, provider_dir: str = "",
                    model_name: str = "") -> bool:
    """这个模型能不能收图 —— 声明 > 查表 > 默认能。

    三层是 `config.yaml` > 模型能力表 > 兜底。
      表是 `providers/model_capabilities.json`（models.dev 的 `modalities`），
      由 `scripts/refetch_window_table.py` 生成。
    没有 manifest 那一层（粒度错：能力逐模型，manifest 逐供应商）。
    兜底方向是 True：图片是现代模型的常见能力（理由见
      `providers/base.py::accepts_images`）。
    """
    return _capability(model_cfg, "supports_vision", "image", True,
                       provider_dir, model_name)[0]


def _resolve_audio(model_cfg: dict, provider_dir: str = "",
                   model_name: str = "") -> bool:
    """这个模型能不能收音频 —— 声明 > 查表 > 默认不能。

    方向与 `_resolve_vision` 相反：能收音频的聊天模型是少数，
      发错会 400。完整理由在 `providers/base.py::accepts_audio`。
    """
    return _capability(model_cfg, "supports_audio", "audio", False,
                       provider_dir, model_name)[0]


def build_provider(provider_dir: Path, config: dict):
    """按 `config` 造一个 provider 实例，把派生量一起交出来。

    1. 读 <provider_dir>/manifest.yaml。
    2. 校验 kind == "provider"。
    3. 动态 import client 模块，找到 *Provider 类。
    4. 用 config 的值实例化（缺省回落到 manifest 默认值）。
    5. 校验它是 BaseProvider 实例。

    为什么连派生量一起回：
      窗口那条链是时间相关的 —— `core/window_estimate.resolve()` 会读
      `observed_lower_bound()`，而它随真实请求只上不下。构造一遍、
      登记元数据时再算一遍，两次可能给出不同的值。
      「来源必须跟着值走」在同一次装载里也成立：元数据里那四个派生量
      （窗口 / 来源 / 看不看图 / 输出上限）必须与送进构造函数的是同一次计算。
这不是为了少算一次，是为了它们不许分叉。

    它不注册任何东西 —— 注册是调用方的事。因为另一条路
      （`core/model_configs.provider_for`）要注册在配置条目 id 下、
      而不是 manifest 的目录名；两个注册名，一个构造器。
    """
    manifest_path = provider_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Provider manifest not found: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)

    if manifest.get("kind") != "provider":
        raise ValueError(
            f"Expected kind='provider' in {manifest_path}, "
            f"got '{manifest.get('kind')}'"
        )

    name = manifest["name"]
    manifest_cfg = manifest.get("config", {})

    # --- import provider 类 ---
    module_name = f"providers.{provider_dir.name}.client"
    module = importlib.import_module(module_name)

    # 按约定找 *Provider 类
    provider_class = None
    for attr_name in dir(module):
        if attr_name.endswith("Provider") and not attr_name.startswith("_"):
            candidate = getattr(module, attr_name)
            if isinstance(candidate, type) and issubclass(candidate, BaseProvider):
                provider_class = candidate
                break

    if provider_class is None:
        raise AttributeError(
            f"No BaseProvider subclass found in {module_name}. "
            f"Expected a class named *Provider that inherits from BaseProvider."
        )

    # --- 合并配置：manifest 默认值 < config.yaml 覆盖 ---
    model_cfg = config.get("model", {})
    rate_limit_cfg = model_cfg.get("rate_limit", {})

    # 窗口：值 + 来源
    #
    # 不能写成匿名的链式 get：`model_cfg.get("context_window",
    # manifest_cfg.get("context_window", 65536))`。声明错了也没有任何地方
    # 知道是哪个环节给的 —— `config.yaml` 声明 131,072，
    # 上游报的真实上限是 1,048,576（差 8 倍），而 `manifest.yaml` 又是第三个
    # 数（65,536）。三个数、零校验。
    #
    # 解析放在 `core/window_estimate.py`（五档来源 + 优先级），
    # 这里只负责递输入 —— 判据不在这条装载链上（一个判定一处）。
    # 两处行为变化，都是有意的：
    #   ① 兜底值 65,536 → 250,000（见 `FALLBACK_WINDOW`）；
    #   ② 多出来的两层（查表 / 演算）会盖过 manifest 那个粗档
    #      —— 优先级表与理由在 `core/window_estimate.py` 的 docstring 里。
    # 保守兜底只能用于"确实不知道"：声明偏小是有代价的（更早压缩），
    # 不能拿它当"不用修窗口"的借口。
    model_name = model_cfg.get("model_name", manifest_cfg.get("default_model", ""))

    # `config.yaml` 的 `max_tokens` 已退役 ——
    #    它是按模型能力定的（`min(limit.output, 64000)`），不是按人喜好定的。
    #    但不许静默忽略一个用户改过的键（静默忽略比不许改更坏）
    #读到它就打一行出声。默认那份 `_DEFAULT_CONFIG` 不带这个键，
    #      所以正常情况下这行不会出现。
    if "max_tokens" in model_cfg:
        print("[bus] config.yaml 的 max_tokens=%r **已退役、不再生效** —— "
              "输出上限现在按模型能力定：min(表里 limit.output, %d)，查不到兜底 %d"
              % (model_cfg["max_tokens"], window_estimate.MAX_OUTPUT_CEILING,
                 window_estimate.MAX_OUTPUT_FALLBACK))

    window_tokens, window_source = window_estimate.resolve(
        provider_dir=provider_dir.name,
        model=model_name,
        config_value=model_cfg.get("context_window"),
        manifest_value=manifest_cfg.get("context_window"),
    )

    # 看得见图吗 —— 判一次，两个地方用
    #   （构造函数的 kwargs 与注册表的元数据）。理由见 `init_kwargs` 那一行。
    # 能力判定同时给出 `(值, 来源)` —— 端配置页要显示"这个模型
    #   能收什么、这个答案是谁给的"（来源必须跟着值走）。
    _resolved_vision, _vision_src = _capability(
        model_cfg, "supports_vision", "image", True,
        provider_dir.name, model_name)
    # 音频能力 —— 同一处判定、同一份派生量。
    _resolved_audio, _audio_src = _capability(
        model_cfg, "supports_audio", "audio", False,
        provider_dir.name, model_name)

    # 组装 kwargs（可选多 key 与限流支持）
    init_kwargs: dict = {
        "api_key": model_cfg.get("api_key", ""),
        "base_url": model_cfg.get("base_url", manifest_cfg.get("base_url", "")),
        "model": model_name,
        "temperature": model_cfg.get("temperature", 0.3),
        # 不读 `config.yaml` 的 `max_tokens` ——
        #   它是按能力定的，不是按人喜好定的：`min(表里的 limit.output, 64000)`，
        #   查不到走兜底 8192（`core/window_estimate.py::resolve_max_tokens`，一处定义）。
        #   一个固定常数（如 4096）会远低于实际能力：
        #   `deepseek-v4-flash` 的上限是 393216，4096 只有它的 1/96，
        #   输出会被拦腰截断。
        "max_tokens": window_estimate.resolve_max_tokens(provider_dir.name, model_name),
        "context_window": window_tokens,
        "window_source": window_source,
        # 价格表按 `(provider, model)` 查，
        # 所以实例必须知道自己是哪一家 —— 目录名就是那张表的键。
        "provider_name": provider_dir.name,
        # 这个模型看不看得见图。
        #
        # 多层，按"声明 > 查表 > 兜底"（顺序承重）：
        #   ① `config.yaml` 的 `model.supports_vision` —— 它坐在 `model_name`
        #      旁边，逐模型的答案（`deepseek-v4-flash` 与
        #      `…-vision-exp` 是两个答案，所以判据必须在这一层）
        #   ② 没写，查能力表（表只能开、不能关）
        #   ③ 仍不知道，`True`（默认开，理由在 `providers/base.py::accepts_images`）
        #   没有 manifest 那一层 —— 粒度错（逐供应商 vs 逐模型），
        #      理由见 `_resolve_vision` 的 docstring
        #
        # 不是 `manifest_cfg.get(...)` 那一行的简单翻版：多层写成一个
        # 链式 get 会让声明错了没人知道是哪个环节给的。
        # 先按 `is None` 判而不是按真假 —— 显式写 `false`
        # 与"没写"必须分得开（`None` vs `False`）。
        "supports_vision": _resolved_vision,
        # 收不收音频（默认 False —— 理由在 `_resolve_audio`）。
        "supports_audio": _resolved_audio,
    }

    # 可选多 key 池
    api_keys = model_cfg.get("api_keys")
    if api_keys and isinstance(api_keys, list):
        init_kwargs["api_keys"] = api_keys

    # 可选客户端限流
    if rate_limit_cfg:
        # 缺省 0 = 不启用客户端限流（详见 config.yaml 的注释）
        init_kwargs["rpm"] = rate_limit_cfg.get("rpm", 0)
        init_kwargs["tpm"] = rate_limit_cfg.get("tpm", 0)

    provider_instance = provider_class(**init_kwargs)

    # 安全检查
    if not isinstance(provider_instance, BaseProvider):
        raise TypeError(
            f"Provider '{name}' ({provider_class.__name__}) "
            f"does not implement BaseProvider."
        )

    #: 派生量只算这一遍，调用方（两个注册点）都拿这一份 —— 见 docstring。
    derived = {
        "name": name,
        "provider_dir": provider_dir.name,
        "model_name": model_name,
        "window_tokens": window_tokens,
        "window_source": window_source,
        "supports_vision": _resolved_vision,
        "supports_vision_source": _vision_src,
        "supports_audio": _resolved_audio,
        "supports_audio_source": _audio_src,
        "max_output_tokens": init_kwargs["max_tokens"],
        "manifest": manifest,
        "manifest_cfg": manifest_cfg,
    }
    return provider_instance, derived


def load_provider(provider_dir: Path, config: dict) -> None:
    """读 manifest -> `build_provider` 造实例 -> 按 manifest 的 `name` 注册。

    它只是 `build_provider` 的一层壳。抽出来的理由是让「配置条目 -> 实例」
      那条新路复用同一段构造逻辑 ——
      否则会出现"第三条各自漂移的实现"（`providers/factory.py` 已经是一条
      漂了的：它不走 `window_estimate.resolve`、没有 `window_source`）。
    """
    instance, d = build_provider(provider_dir, config)
    name = d["name"]
    manifest, manifest_cfg = d["manifest"], d["manifest_cfg"]

    register_provider(name, instance)

    # --- 把 provider 元数据登记进注册表 ---
    from bus.registry import register_provider_metadata

    register_provider_metadata(name, {
        "name": name,
        "api_mode": manifest_cfg.get("api_mode", "openai_compat"),
        "description": manifest.get("description", ""),
        "version": manifest.get("version", "0.1.0"),
        "default_model": manifest_cfg.get("default_model", ""),
        "base_url": manifest_cfg.get("base_url", ""),
        # 这里登记的是真正生效的那个窗口（不是 manifest 里那个数）——
        # 否则模型页显示的与压缩闸门用的会是两个数（"来源必须跟着值走"，
        # 只是这次分叉在"显示"与"使用"之间）。`window_source` 一起带上。
        "context_window": d["window_tokens"],
        "window_source": d["window_source"],
        "supports_function_calling": manifest_cfg.get("supports_function_calling", True),
        "supports_streaming": manifest_cfg.get("supports_streaming", True),
        # 注册表这一格与送进构造函数的同一个值（`_resolve_vision`）——
        #   两处各写一份链式 get 会让"显示的能力"与"运行时的判定"分叉
        #   （"来源必须跟着值走"，只是这次分叉在显示与使用之间）。
        "supports_vision": d["supports_vision"],
        # 来源也跟着值走 —— 端配置页显示"谁给的这个答案"。
        "supports_vision_source": d["supports_vision_source"],
        # 与送进构造函数的同一个值（来源跟着值走）。
        "supports_audio": d["supports_audio"],
        "supports_audio_source": d["supports_audio_source"],
        "supports_thinking": manifest_cfg.get("supports_thinking", False),
        # 同一处来源 —— 注册表这一格是显示给人看的，
        #   与送进构造函数的那个值必须同源（否则"显示的能力"与"运行的判定"分叉）。
        #   直接取 `build_provider` 那一次的结果，不要在这里第二次计算。
        "max_output_tokens": d["max_output_tokens"],
    })

    print(
        f"[bus] Provider loaded: {name} "
        f"(model={instance._model}, "
        f"window={instance.context_window})"
    )
