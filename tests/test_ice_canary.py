"""`ICE` 网络侧（金丝雀 tripwire）的机械读者。

它守的四件：

  1. 接了没有 —— 两扇门（`web_fetch` / `send_email`）都调了
     `check_outbound`。判据：「我写了这个机制」≠「这条路径上会调它」。
  2. 次序 —— 金丝雀必须排在 `check_url_egress` 之前。
     诱饵域名是 `.invalid`（永远解析不到），排后面的话地板会在
     "解析失败"那一支先把它吞掉，这条闸永远不会响。
  3. 开关方向 —— 命中，阻断；读不到设置，关（不凭空启用一套会拦人的东西）。
  4. 报警真的发得出去（一条事件）—— 只有控制台的报警在 WebUI 上等于没有。

ASCII-only 输出（Windows 控制台默认 GBK，emoji 会让整个运行崩掉）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_ice_canary.py
"""

import json
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_PROJECT_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "tests"))

from core.ice import canary as C               # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list = []


def check(label: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [ok]   %s" % label)
    else:
        _FAIL += 1
        _FAILED.append(label)
        print("  [FAIL] %s%s" % (label, ("  -- " + detail) if detail else ""))


def _write_settings(enabled: bool, info: bool = True, net: bool = True) -> None:
    p = Path("data/settings.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"ice": {"enabled": enabled, "info_protection": info,
                 "net_protection": net}}, ensure_ascii=False), encoding="utf-8")


# 1. 诱饵值本身

def test_the_canary_values_are_the_two_forms():
    check("★ 域名形态用保留 TLD `.invalid`（RFC 2606：保证永远解析不到）",
          C.CANARY_DOMAIN.endswith(".invalid"), C.CANARY_DOMAIN)
    check("★ 唯一串带前缀（一眼看得出是框架埋的）",
          C.CANARY_STRING.startswith("CRANNIA-CANARY-"), C.CANARY_STRING)
    check("两种形态都在名单里", len(C.CANARY_VALUES) == 2)


def test_find_hits():
    check("域名命中", C.find_hits("https://x/" + C.CANARY_DOMAIN + "/p")
          == (C.CANARY_DOMAIN,))
    check("唯一串命中", C.find_hits("收件人：" + C.CANARY_STRING)
          == (C.CANARY_STRING,))
    check("★ 大小写不敏感（域名本来就不敏感；唯一串改一个大小写仍是同一个意图）",
          C.find_hits(C.CANARY_STRING.lower()) == (C.CANARY_STRING,))
    check("两个都出现 -> 都给出来",
          len(C.find_hits(C.CANARY_DOMAIN + " " + C.CANARY_STRING)) == 2)
    check("普通文本 -> 空（误伤面≈零）",
          C.find_hits("https://example.com/报表.xlsx") == ())
    check("空串 -> 空", C.find_hits("") == () and C.find_hits(None) == ())


# 2. 开关（总闸 且 网络层）

def test_switch_requires_BOTH_the_master_and_the_net_key():
    from _sandbox import sandbox
    with sandbox():
        _write_settings(enabled=True, net=True)
        check("★ 总闸开 + 网络层开 -> 生效", C.is_enabled() is True)
        _write_settings(enabled=True, net=False)
        check("★ 网络层关 -> 不生效（§6.9：关网络层 = 无金丝雀）",
              C.is_enabled() is False)
        _write_settings(enabled=False, net=True)
        check("★ 总闸关 -> 不生效（总闸对**所有层**生效）", C.is_enabled() is False)
        _write_settings(enabled=True, info=False, net=True)
        check("★ 信息层关**不影响**网络层（两组开关互不依赖）",
              C.is_enabled() is True)


def test_missing_or_broken_settings_means_OFF():
    from _sandbox import sandbox
    with sandbox():
        check("★ 没有设置文件 -> 关（同 ICE 的全局兜底方向）",
              C.is_enabled() is False)
        p = Path("data/settings.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ not json", encoding="utf-8")
        check("★ 设置文件坏了 -> 关（绝不因此弄挂一次调用，也不凭空启用）",
              C.is_enabled() is False)


# 3. 出站闸

def test_check_outbound_blocks_and_returns_a_receipt():
    from _sandbox import sandbox
    with sandbox():
        _write_settings(enabled=True, net=True)
        r = C.check_outbound("https://" + C.CANARY_DOMAIN + "/x", where="web_fetch")
        check("★ 命中 -> 给回执（不是 None）", isinstance(r, dict), repr(r))
        if isinstance(r, dict):
            check("★ 回执说清了「被阻断」", r.get("blocked") is True
                  and r.get("success") is False, repr(r))
            check("★ 带上 canary 标记（调用方/读者据此认得出这一支）",
                  r.get("canary") is True)
            check("★ 正文里点破了「这多半是注入」", "注入" in r.get("error", ""))
            check("★ 并且明说**不要重试**", "不要重试" in r.get("error", ""))
        check("没命中 -> None（正常路径）",
              C.check_outbound("https://example.com/x", where="web_fetch") is None)


def test_check_outbound_is_off_when_the_net_switch_is_off():
    from _sandbox import sandbox
    with sandbox():
        _write_settings(enabled=True, net=False)
        check("★ 网络层关着 -> 即使带诱饵也放行（开关**真的**管用）",
              C.check_outbound("https://" + C.CANARY_DOMAIN + "/x",
                               where="web_fetch") is None)


def test_the_alarm_really_reaches_the_event_bus():
    """只有控制台的报警，在 WebUI 上等于没有报警。"""
    from _sandbox import sandbox
    import core.events as EV

    seen: list = []

    class _FakeBus:
        def emit(self, evt_type, session_key="", **data):
            seen.append((evt_type, session_key, data))

    real = EV.get_event_bus
    EV.get_event_bus = lambda: _FakeBus()
    try:
        with sandbox():
            _write_settings(enabled=True, net=True)
            C.note_trip("web_fetch", (C.CANARY_DOMAIN,), session_key="s-1")
    finally:
        EV.get_event_bus = real

    check("★ 发了一条事件", len(seen) == 1, repr(seen))
    if seen:
        check("★ 类型是 ice.canary", seen[0][0] == EV.EVT_ICE_CANARY, repr(seen[0][0]))
        check("★ 带上了会话号（否则 SSE 找不到订阅者）", seen[0][1] == "s-1")
        check("★ 载荷里有 where 与 hits",
              seen[0][2].get("where") == "web_fetch"
              and seen[0][2].get("hits") == [C.CANARY_DOMAIN], repr(seen[0][2]))
    check("★ 它是 durable（事后翻日志要数得出来）",
          EV.EVT_ICE_CANARY in EV.DURABLE_TYPES)


# 4. 接线（"写了" ≠ "会调它"）

def _src(rel: str) -> str:
    return (_PROJECT_ROOT / rel).read_text(encoding="utf-8")


def test_every_door_calls_the_canary():
    for rel, label in (("mcpx/servers/web_search.py", "web_fetch"),
                       ("skills/mail_parser/handler.py", "send_email")):
        src = _src(rel)
        check("★ %s：调了 `check_outbound(`" % label, "check_outbound(" in src)


def test_the_canary_comes_BEFORE_the_floor():
    """次序是判据的一部分：诱饵域名是 `.invalid`、永远解析不到，
    排在地板后面就会被"解析失败"那一支先吞掉，这条闸永远不响。"""
    for rel, label in (("mcpx/servers/web_search.py", "web_fetch"),):
        src = _src(rel)
        i_canary = src.find("check_outbound(")
        i_floor = src.find("check_url_egress(")
        check("★★ %s：金丝雀在 `check_url_egress` **之前**" % label,
              i_canary >= 0 and i_floor >= 0 and i_canary < i_floor,
              "canary@%d floor@%d" % (i_canary, i_floor))


def test_the_web_fetch_canary_is_inside_the_hop_loop():
    src = _src("mcpx/servers/web_search.py")
    m = re.search(r"for _hop in range\(.*?\n(.*?)\n    else:", src, re.S)
    body = m.group(1) if m else ""
    check("★ 逐跳都要过（跳转目标里也可能带诱饵）", "check_outbound(" in body,
          "" if body else "循环体没抓到")


def test_the_send_email_scan_covers_the_text_fields():
    """缺口要写在案上：附件不扫（诱饵当附件发出去时不响）。"""
    src = _src("skills/mail_parser/handler.py")
    m = re.search(r"check_outbound\((.*?)\)", src, re.S)
    args = m.group(1) if m else ""
    for f in ("to", "cc", "bcc", "subject", "body"):
        check("  ★ 扫到 %s" % f, f in args, args[:200])
    check("  [!] 并**如实标注**了附件不在范围内", "附件不在扫描范围" in src)


def main() -> int:
    tests = [
        test_the_canary_values_are_the_two_forms,
        test_find_hits,
        test_switch_requires_BOTH_the_master_and_the_net_key,
        test_missing_or_broken_settings_means_OFF,
        test_check_outbound_blocks_and_returns_a_receipt,
        test_check_outbound_is_off_when_the_net_switch_is_off,
        test_the_alarm_really_reaches_the_event_bus,
        test_every_door_calls_the_canary,
        test_the_canary_comes_BEFORE_the_floor,
        test_the_web_fetch_canary_is_inside_the_hop_loop,
        test_the_send_email_scan_covers_the_text_fields,
    ]
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("%s 没抛异常（抛了 %s）" % (fn.__name__, type(exc).__name__),
                  False, str(exc))
    print("")
    if _FAILED:
        print("FAILED:")
        for f in _FAILED:
            print("  - %s" % f)
    print("%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
