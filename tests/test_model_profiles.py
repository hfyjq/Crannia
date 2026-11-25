"""模型能力差异表的机械读者。

钉五组判据：

  1. 16 家 provider 全有画像，端配置 12 张卡映射到的 provider 都有行。
  2. 模型级覆盖按精确 / 最长包含匹配，代际差异（Gemini 3、Claude 4.7）真的生效。
  3. unknown 与 false 分开：查不到不能当"不支持"用。
  4. 兼容位与能力不匹配策略都在表里，不散在代码分支。
  5. 表里没有"自动统计回写"的字段或逻辑。

ASCII-only 输出（GBK 控制台）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_model_profiles.py
"""

import io
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import model_profiles as MP  # noqa: E402

_PASS = 0
_FAIL = 0


def check(desc, ok, note=""):
    global _PASS, _FAIL
    print("  [%s] %s%s" % ("ok" if ok else "!!", desc,
                           ("  -- " + note) if note and not ok else ""))
    if ok:
        _PASS += 1
    else:
        _FAIL += 1


def _reset():
    os.chdir(_PROJECT_ROOT)
    MP.reset_cache()


def test_every_provider_is_covered():
    _reset()
    window = json.loads((_PROJECT_ROOT / "providers" / "window_table.json").read_text(
        encoding="utf-8"))["providers"]
    table = json.loads((_PROJECT_ROOT / "providers" / "model_profiles.json").read_text(
        encoding="utf-8"))["providers"]
    missing = [p for p in window if p not in table]
    check("窗口表 16 家在能力表里都有行", not missing, str(missing))

    from core.model_configs import CARD_PROVIDER
    cards_missing = [f"{c}->{p}" for c, p in CARD_PROVIDER.items() if p not in table]
    check("12 张端配置卡映射到的 provider 都有行（含「其他」）",
          not cards_missing, str(cards_missing))
    for p in window:
        q = MP.resolve(provider=p, model="probe")
        if not isinstance(q, MP.ModelProfile):
            check("%s 可解析" % p, False)
    check("16 家逐行可解析，没有死行", True)


def test_tool_call_is_not_assumed_when_unknown():
    _reset()
    a = MP.resolve(provider="anthropic", model="x")
    u = MP.resolve(provider="unknown-provider", model="x")
    check("anthropic 明说支持工具调用", a.can("tool_call") is True)
    check("没查过的 provider 是 None，不是 False", u.can("tool_call") is None,
          str(u.can("tool_call")))
    check("unknown 时策略落在 error（不能静默照发工具）",
          u.on_unsupported.get("tool_call") == "error",
          str(u.on_unsupported))
    check("媒体策略是降级，不是直接丢", u.on_unsupported.get("media") == "degrade")


def test_generation_specific_parameter_bans():
    """不同代际的参数禁区必须按模型名生效，而不是写在调用方。"""
    _reset()
    old = MP.resolve(provider="anthropic", model="claude-sonnet-4-6")
    new = MP.resolve(provider="anthropic", model="claude-opus-4-7")
    check("普通 Claude 允许 temperature", old.parameter("temperature") is True)
    check("Claude 4.7 禁 temperature", new.parameter("temperature") is False,
          str(new.parameter("temperature")))
    check("4.7 的匹配是精确命中", new.matched == "claude-opus-4-7", new.matched)

    g_old = MP.resolve(provider="gemini", model="gemini-2-5-pro")
    g_new = MP.resolve(provider="gemini", model="gemini-3-pro-preview")
    check("Gemini 2.5 未禁 temperature", g_old.parameter("temperature") is True)
    check("Gemini 3 禁 temperature 且按最长包含命中",
          g_new.parameter("temperature") is False and g_new.matched == "gemini-3",
          "%s/%s" % (g_new.parameter("temperature"), g_new.matched))


def test_compat_fields_are_data():
    """wire 差异必须在表里，不能靠调用方按 provider 写分支。"""
    _reset()
    ds = MP.resolve(provider="deepseek", model="deepseek-reasoner")
    check("DeepSeek 的推理字段是 reasoning_content",
          ds.compat_of("reasoning_field") == "reasoning_content")
    check("DeepSeek 的输出上限字段是 max_tokens",
          ds.compat_of("max_tokens_field") == "max_tokens")
    check("DeepSeek 的采样参数禁发",
          ds.parameter("temperature") is False and ds.parameter("top_p") is False)

    an = MP.resolve(provider="anthropic", model="x")
    check("Anthropic 的思考字段是 thinking",
          an.compat_of("reasoning_field") == "thinking")
    check("Anthropic 的 thinking block 绑定是显式 true",
          an.compat.get("supports_thinking_block_binding") is True,
          str(an.compat))
    qw = MP.resolve(provider="qwen", model="x")
    check("Qwen 的缓存形状是 alibaba", qw.compat_of("cache_format") == "alibaba")


def test_three_state_values_are_preserved():
    """表里写了 false 就是 false，没写就是 unknown，不能混成一个 falsy。"""
    _reset()
    table = json.loads((_PROJECT_ROOT / "providers" / "model_profiles.json").read_text(
        encoding="utf-8"))
    check("全局默认用字符串表达 unknown 的位置保持原样",
          table["_default"]["tool_call"] == "unknown")
    check("明确 false 的位置写的是 JSON false",
          table["providers"]["anthropic"]["models"]["claude-opus-4-7"]
          ["parameters"]["temperature"] is False)
    src = (_PROJECT_ROOT / "core" / "model_profiles.py").read_text(encoding="utf-8")
    check("解析器把 unknown / false 分开（有 _tri 且认 None）",
          "def _tri" in src and "return None" in src)
    check("没有统计回写接口（不提供 write / observe 方法）",
          "def write" not in src and "def observe" not in src)

def test_consumers_are_real_not_just_data():
    """画像要真的影响请求：参数禁区、工具闸、wire 清洗三处。"""
    _reset()
    from core import model_profiles as M

    params = {"temperature": 0.3, "top_p": 0.9, "max_tokens": 8192}
    check("Claude 4.7 的采样参数被删",
          "temperature" not in M.filter_request_params(
              params, "anthropic", "claude-opus-4-7"))
    check("DeepSeek 的采样参数被删",
          "top_p" not in M.filter_request_params(
              params, "deepseek", "deepseek-reasoner"))
    check("未知模型不删参数（不破坏自建端）",
          M.filter_request_params(params, "custom", "x") == params)
    check("max_tokens 字段按画像换名",
          "max_completion_tokens" in M.filter_request_params(
              params, "openai", "gpt-5"))

    msgs = [{"role": "assistant", "content": "",
             "reasoning_content": "上一家的思考",
             "tool_calls": [{"id": "t1"}]}]
    out = M.sanitize_for_wire(msgs, "openai", "gpt-5")
    check("目标不认 reasoning_content 时剥掉",
          "reasoning_content" not in out[0], str(out[0]))
    check("原消息不被改（它还要落库）",
          "reasoning_content" in msgs[0] and msgs[0]["content"] == "")
    keep = M.sanitize_for_wire(msgs, "deepseek", "deepseek-reasoner")
    check("目标自己认这个字段时保留",
          "reasoning_content" in keep[0], str(keep[0]))
    check("幂等：再洗一次结果相同",
          M.sanitize_for_wire(msgs, "openai", "gpt-5") == out)


def test_unknown_tool_gate_is_not_tool_table_wipe():
    """unknown 时照发工具表；拒绝幻觉调用是执行侧另一道闸。"""
    _reset()
    from core import model_profiles as M
    check("未知模型照发工具表", M.accepts_tools("custom", "x") is True)
    check("未知模型的幻觉工具按策略拒绝执行",
          M.rejects_unknown_tool("custom", "x") is True)
    check("明确支持工具的仍然发",
          M.accepts_tools("anthropic", "claude-sonnet-4-6") is True)

def test_finish_reason_gate():
    """半截流只能对明确会报结束信号的供应商判，读不到不等于断。"""
    _reset()
    from core import model_profiles as M
    check("OpenAI 兼容那批会报 finish_reason",
          M.reports_finish_reason("openai", "gpt-5") is True)
    check("Anthropic 会报 stop_reason",
          M.reports_finish_reason("anthropic", "x") is True)
    check("自建端没表态，不当成会报（避免每轮假续写）",
          M.reports_finish_reason("custom", "x") is False)


def test_custom_relay_is_not_broken():
    """「其他」卡映射到 openai：未知模型必须保持旧 wire 形状。

    这一格是复核时抓到的：「其他」与 GPT 共用 provider `openai`，
    若把 `max_completion_tokens` / `reports_finish_reason` 写在 provider
    默认上，所有自建中转站都会被改坏或每轮假续写。
    """
    _reset()
    from core import model_profiles as M
    params = {"max_tokens": 8192, "temperature": 0.3}
    got = M.filter_request_params(params, "openai", "my-relay-model")
    check("未知模型仍用 max_tokens", got.get("max_tokens") == 8192, str(got))
    check("未知模型不会被换成 max_completion_tokens",
          "max_completion_tokens" not in got, str(got))
    check("官方模型仍然换名",
          "max_completion_tokens" in M.filter_request_params(
              params, "openai", "gpt-5"))
    check("未知模型不被当成会报 finish_reason",
          M.reports_finish_reason("openai", "my-relay-model") is False)
    check("官方模型仍然算会报",
          M.reports_finish_reason("openai", "gpt-5") is True)
    check("本地 ollama 不被当成会报",
          M.reports_finish_reason("ollama", "qwen2.5") is False)


def test_sanitize_handles_broken_history():
    """坏历史：非 dict 消息不许把下标带偏；多条悬空调用各补一条。"""
    _reset()
    from core import model_profiles as M
    ragged = [None, {"role": "assistant", "content": "",
                     "reasoning_content": "x"}]
    out = M.sanitize_for_wire(ragged, "openai", "gpt-5")
    check("非 dict 消息不崩、长度不变", len(out) == len(ragged), repr(out))
    check("空 assistant 补了空格", out[1].get("content") == " ", repr(out))

    msgs = [{"role": "assistant", "content": "",
             "tool_calls": [{"id": "t2"}, {"id": "t1"}]},
            {"role": "user", "content": "接着来"}]
    fixed = M.sanitize_for_wire(msgs, "openai", "gpt-5")
    roles = [m.get("role") for m in fixed]
    check("两条悬空调用各补一条结果",
          roles == ["assistant", "tool", "tool", "user"], repr(roles))
    check("占位结果紧跟在声明它的 assistant 后面",
          [m.get("tool_call_id") for m in fixed[1:3]] == ["t1", "t2"], repr(fixed))
    check("再洗一次幂等", M.sanitize_for_wire(fixed, "openai", "gpt-5") == fixed)


def main() -> int:
    print("=" * 62)
    print("Model profiles: 16 providers / generation differences / compatibility")
    print("=" * 62)
    try:
        for fn in (test_every_provider_is_covered,
                   test_tool_call_is_not_assumed_when_unknown,
                   test_generation_specific_parameter_bans,
                   test_compat_fields_are_data,
                   test_three_state_values_are_preserved,
                   test_consumers_are_real_not_just_data,
                   test_unknown_tool_gate_is_not_tool_table_wipe,
                   test_finish_reason_gate,
                   test_custom_relay_is_not_broken,
                   test_sanitize_handles_broken_history):
            print("\n-- %s --" % fn.__name__)
            fn()
    finally:
        os.chdir(_PROJECT_ROOT)
        MP.reset_cache()
    print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
