"""图片卸载的隔离与批量边界。

钉四组判据：

  1. 接管截图里最近三张受保护，模型用 read_file 翻旧图不能把它们顶掉。
  2. 翻出来的旧图先清，普通图后清；同一来源内部从最老开始（序号是模型唯一的指代）。
  3. 没到批量边界、也没超硬上限时一轮都不改，前缀纯追加；到边界时一次性退到 3。
  4. 不往消息或图片块上写内部字段 —— 消息级私有字段没有统一的 wire 剥除处。

用 tool_call_id 反查工具名来认来源，这是历史本来就有的字段（落库进 extra_json、
装载时还原），不需要新机制。

ASCII-only 输出（GBK 控制台）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_image_offload_policy.py
"""

import io
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core import image_input as II  # noqa: E402
from tests._sandbox import sandbox  # noqa: E402

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


def _png(size=(24, 24)):
    buf = io.BytesIO()
    from PIL import Image
    Image.new("RGB", size, (180 + size[0] % 50, 100, 60)).save(buf, format="PNG")
    return buf.getvalue()


def _tool_turn(messages, name, call_id, block):
    """一条 assistant(tool_calls) + 一条 tool 结果，形状与 agent_loop 一致。"""
    messages.append({
        "role": "assistant", "content": "",
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": "{}"}}],
    })
    messages.append({"role": "tool", "tool_call_id": call_id,
                     "content": [{"type": "text", "text": "观察"}, block]})


def _imgs(messages):
    return [(mi, pi)
            for mi, m in enumerate(messages)
            if isinstance(m.get("content"), list)
            for pi, p in enumerate(m["content"])
            if isinstance(p, dict) and p.get("type") == "image_url"]


def _contents(messages):
    """逐条消息的内容快照（逐项比较才能判前缀，字符串比会被 `]` 骗到）。"""
    return [json.dumps(m.get("content"), ensure_ascii=False, sort_keys=True)
            for m in messages]


def test_source_is_read_from_tool_calls():
    with sandbox():
        msgs = []
        _tool_turn(msgs, "computer", "c1", II.prepare_bytes(_png()).block)
        _tool_turn(msgs, "read_file", "r1", II.prepare_bytes(_png((26, 26))).block)
        names = II._tool_names_by_call(msgs)
        check("tool_call_id 能反查到 computer", names.get("c1") == "computer", str(names))
        check("也能反查到 read_file", names.get("r1") == "read_file", str(names))
        check("映射不写回消息（零新增字段）",
              all(not str(k).startswith("_framework")
                  for m in msgs for k in m))


def test_recent_screenshots_survive_fetched_old_images():
    """20 步每步翻一张旧图，最近三张截图必须始终在。"""
    with sandbox():
        msgs = []
        for i in range(3):
            _tool_turn(msgs, "computer", "c%d" % i,
                       II.prepare_bytes(_png((30 + i, 30))).block)
        kept = set(_imgs(msgs))

        for i in range(20):
            block = II.prepare_bytes(_png((60 + i, 60))).block
            _tool_turn(msgs, "read_file", "r%d" % i, block)
            II.offload_old_images(msgs, keep=3)
            live = set(_imgs(msgs))
            check("第 %d 次翻图后三张截图仍在" % (i + 1),
                  kept <= live, "缺 %s" % (kept - live))
        check("翻出来的旧图被清干净（只留三张截图）",
              len(live) == 3 and live == kept, str(sorted(live)))


def test_fetched_images_go_before_normal_ones():
    """先清 read_file 翻出来的，再清普通图；同来源从最老开始。"""
    with sandbox():
        msgs = []
        # 一张普通用户贴图
        msgs.append({"role": "user", "content": [
            {"type": "text", "text": "看看"},
            II.prepare_bytes(_png((40, 40))).block]})
        # 三张最近截图（受保护）
        for i in range(3):
            _tool_turn(msgs, "computer", "c%d" % i,
                       II.prepare_bytes(_png((41 + i, 41))).block)
        # 一张更早的普通图，夹在三张截图之前
        msgs.insert(1, {"role": "user", "content": [
            {"type": "text", "text": "更早"},
            II.prepare_bytes(_png((39, 39))).block]})
        # 紧接着翻两张旧图
        for i in range(2):
            _tool_turn(msgs, "read_file", "r%d" % i,
                       II.prepare_bytes(_png((70 + i, 70))).block)

        II.offload_old_images(msgs, keep=3)
        live = _imgs(msgs)
        check("只剩三张受保护的截图", len(live) == 3, str(live))
        texts = [m["content"][1].get("text", "")
                 for m in msgs if isinstance(m.get("content"), list)
                 and len(m["content"]) > 1
                 and m["content"][1].get("type") == "text"]
        check("清掉的那几句话都带可取回的路径",
              all("data/attachments/" in t for t in texts), repr(texts)[:200])


def test_pure_append_until_the_boundary():
    """离边界还远、也没超硬上限时，一个字节都不改。"""
    with sandbox():
        msgs = []
        prev = None
        for i in range(5):
            _tool_turn(msgs, "computer", "c%d" % i,
                       II.prepare_bytes(_png((32 + i, 32))).block)
            II.offload_old_images(msgs, keep=3, batch_every=25, max_inline=12)
            now = _contents(msgs)
            if prev is not None:
                check("第 %d 轮是上一轮的严格前缀" % (i + 1),
                      now[:len(prev)] == prev and len(now) > len(prev))
            prev = now
        check("五轮下来五张截图一张没卸", len(_imgs(msgs)) == 5, str(len(_imgs(msgs))))


def test_batch_eviction_recovers_to_three():
    """到 25 张截图那一轮一次性退到 3，之后又继续纯追加。"""
    with sandbox():
        msgs = []
        boundary_seen = 0
        for i in range(25):
            _tool_turn(msgs, "computer", "c%d" % i,
                       II.prepare_bytes(_png((34 + i, 34))).block)
            before = _contents(msgs)
            II.offload_old_images(msgs, keep=3, batch_every=25, max_inline=12)
            after = _contents(msgs)
            if after != before:
                boundary_seen += 1
            if i in (12, 24):
                check("第 %d 张触发了批量卸载" % (i + 1), after != before)
        check("第 25 张之后只剩最近三张", len(_imgs(msgs)) == 3,
              str(len(_imgs(msgs))))
        check("批量轮至少出现两次（硬上限与 25 次边界）",
              boundary_seen >= 2, str(boundary_seen))


def test_no_takeover_images_keeps_the_old_behavior():
    """没有接管截图时不启用批量：4 张普通图仍然只留最近 3 张。"""
    with sandbox():
        msgs = []
        for i in range(4):
            msgs.append({"role": "user", "content": [
                {"type": "text", "text": "第%d张" % i},
                II.prepare_bytes(_png((44 + i, 44))).block]})
        II.expand_attachments(msgs)
        check("普通贴图仍按 keep=3 裁", len(_imgs(msgs)) == 3,
              str(len(_imgs(msgs))))


def test_expand_forwards_the_batch_policy():
    """调用点一条路：expand_attachments 默认就带批量参数。"""
    with sandbox():
        msgs = []
        for i in range(6):
            _tool_turn(msgs, "computer", "c%d" % i,
                       II.prepare_bytes(_png((46 + i, 46))).block)
        II.expand_attachments(msgs)
        check("默认批量边界下 6 张截图还都留着",
              len(_imgs(msgs)) == 6, str(len(_imgs(msgs))))
        check("常量是 3 / 25 / 12",
              (II.KEEP_IMAGES_IN_REQUEST, II.BATCH_EVERY_OBSERVATIONS,
               II.MAX_INLINE_IMAGES) == (3, 25, 12))


def test_eviction_none_keeps_every_image():
    """策略说 none 时不做客户端淘汰；图仍在，pin 也仍在。"""
    with sandbox():
        msgs = []
        for i in range(6):
            _tool_turn(msgs, "computer", "c%d" % i,
                       II.prepare_bytes(_png((90 + i, 90))).block)
        II.expand_attachments(msgs, eviction="none")
        check("eviction=none 时一张都没卸", len(_imgs(msgs)) == 6,
              str(len(_imgs(msgs))))


def main() -> int:
    print("=" * 62)
    print("Image offload: pinned screenshots / transient first / batch boundary")
    print("=" * 62)
    for fn in (test_source_is_read_from_tool_calls,
               test_recent_screenshots_survive_fetched_old_images,
               test_fetched_images_go_before_normal_ones,
               test_pure_append_until_the_boundary,
               test_batch_eviction_recovers_to_three,
               test_no_takeover_images_keeps_the_old_behavior,
               test_expand_forwards_the_batch_policy,
               test_eviction_none_keeps_every_image):
        print("\n-- %s --" % fn.__name__)
        fn()
    print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
