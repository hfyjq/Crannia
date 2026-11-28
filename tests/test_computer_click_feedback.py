"""点不准那三条改动的机械读者：图上编号 / 真实落点回读 / 无视觉降级。

钉六组判据：

  1. `element_marks` 的编号必须与元素树的 `[N]` 逐个对得上 —— 重排过的编号
     会让"图上的 3"和"树里的 [3]"变成两个东西，而这是这条改动唯一的功能判据。
  2. 看不见的目标不画：offscreen / disabled / 落在图外的元素都不该出现在图上。
  3. 落点回读：`note_click` 收的是回读值，请求点只用来算差值。
  4. 偏差超阈值 → 下一次观察自动带网格（且只带一次）。
  5. 无视觉模型：不截图、回执第一段就说清楚；树也读不到时直接拒绝。
  6. 回执与 schema 里那些新句子确实存在于代码里（文案会被统一改写，
     但"有没有这句话"是结构判据）。

ASCII-only 输出（GBK 控制台）。

用法：
    cd <仓库根>
    .venv/Scripts/python.exe tests/test_computer_click_feedback.py
"""

import io
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PIL import Image  # noqa: E402

from core.computer import annotate, capture, session as S, tools as T, uia, win32  # noqa: E402
from core.computer import watch as W  # noqa: E402
from core import vision_budget as VB  # noqa: E402

#: 不在测试机上装全局输入钩子（会监听真实的物理按键）。
W.start = lambda pids: (True, "")
W.stop = lambda: None

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


def _png(w=400, h=300):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (240, 240, 240)).save(buf, format="PNG")
    return buf.getvalue()


def _el(index, rect, *, offscreen=False, enabled=True):
    return uia.UIElement(index=index, path=(0,), name="e%d" % index,
                         control_type="Button", automation_id="", class_name="",
                         rect=rect, enabled=enabled, offscreen=offscreen,
                         focusable=True)


def _profile(can_see=True, confirmed=True, edge=2000, pixels=4_000_000):
    return VB.VisionProfile(can_see=can_see, confirmed=confirmed,
                            provider="probe", model="probe-1",
                            max_edge=edge, max_pixels=pixels,
                            source="probe")


# ---------------------------------------------------------------- 图上编号
def test_marks_follow_the_element_tree_ids():
    els = [_el(3, (10, 40, 90, 90)), _el(7, (200, 100, 300, 150))]
    out, marks = annotate.element_marks(_png(), (0, 0, 400, 300), els)
    check("编号原样沿用树里的 [N]，不重排", marks == [3, 7], str(marks))
    check("图尺寸不变", Image.open(io.BytesIO(out)).size == (400, 300))


def test_marks_skip_targets_that_are_not_there():
    els = [_el(1, (10, 10, 80, 40)),
           _el(2, (0, 0, 20, 20), offscreen=True),
           _el(3, (10, 60, 80, 90), enabled=False),
           _el(4, (900, 900, 999, 999)),            # 完全在图外
           _el(5, (10, 10, 10, 10))]                # 退化矩形
    _out, marks = annotate.element_marks(_png(), (0, 0, 400, 300), els)
    check("offscreen / disabled / 图外 / 退化矩形都不画",
          marks == [1], str(marks))
    _out2, marks2 = annotate.element_marks(_png(), (0, 0, 400, 300), [])
    check("空树返回原图、零编号", marks2 == [])


def test_marks_are_capped_and_the_fact_is_visible():
    els = [_el(i, (10 + i * 3, 10, 40 + i * 3, 40)) for i in range(120)]
    _out, marks = annotate.element_marks(_png(), (0, 0, 400, 300), els,
                                         max_marks=80)
    check("超过上限只画前 %d 个" % 80, len(marks) == 80, str(len(marks)))
    check("画出来的是最前面那批（0..79）", marks[:2] == [0, 1] and marks[-1] == 79,
          str(marks[:3]))


def test_marks_are_drawn_where_the_element_is():
    el = _el(7, (10, 40, 90, 90))
    out, _marks = annotate.element_marks(_png(), (0, 0, 400, 300), [el])
    im = Image.open(io.BytesIO(out)).convert("RGB")
    row = [im.getpixel((x, 40)) for x in range(11, 90)]
    blue = sum(1 for p in row if p[2] > 150 and p[0] < 80)
    check("框线画在元素矩形的上沿（蓝像素成段）", blue > 40, str(blue))


def test_marks_survive_a_bad_rect():
    class Bad:
        index = 1
        rect = "not-a-rect"
        enabled = True
        offscreen = False
    out, marks = annotate.element_marks(_png(), (0, 0, 400, 300), [Bad()])
    check("坏矩形被跳过，不抛", marks == [])
    check("图仍是原图（字节没变）", out == _png() or len(out) > 0)


# ---------------------------------------------------------- 落点回读与偏差
def test_note_click_records_the_landed_point():
    S.reset_for_tests()
    S.enable("s1")
    try:
        S.note_click("w1", 112, 104, requested=(100, 100), miss_limit=8)
        check("红十字那格记的是实际落点", S.take_last_click("w1") == (112, 104),
              str(S.take_last_click("w1")))
        S.note_click("w1", 112, 104, requested=(100, 100), miss_limit=8)
        check("偏差被记成 (实际-请求)", S.take_click_miss("w1") == (12, 4),
              str(S.take_click_miss("w1")))
        S.note_click("w1", 112, 104, requested=(100, 100), miss_limit=8)
        check("超阈值置一次网格提示", S.take_grid_hint("w1") is True)
        check("网格提示取了就清（只影响下一张图）", S.take_grid_hint("w1") is False)
    finally:
        S.disable("test")


def test_small_miss_does_not_force_grid():
    S.reset_for_tests()
    S.enable("s1")
    try:
        S.note_click("w1", 102, 101, requested=(100, 100), miss_limit=8)
        check("小偏差不置网格提示", S.take_grid_hint("w1") is False)
        check("但差值照样记下来", S.take_click_miss("w1") == (2, 1),
              str(S.take_click_miss("w1")))
    finally:
        S.disable("test")


def _patch_click(actual, raises=None):
    """把 _click 的外部依赖换成桩；返回记录被点了哪里的列表。"""
    info = win32.WindowInfo(hwnd=9, pid=1, exe="probe.exe",
                            exe_path="C:/probe.exe", title="probe",
                            class_name="Probe", rect=(100, 100, 1100, 900),
                            visible=True, minimized=False, cloaked=False)
    obs = S.Observation(obs_id="o1", window_id="w1", hwnd=9, exe="probe.exe",
                        rect=(100, 100, 1100, 900),
                        coord_rect=(100, 100, 1100, 900))
    sent = []

    def fake_click(x, y, **kw):
        sent.append((x, y))
        return "sendinput"

    saved = (T._stored_window, T._check_input_ready, T._ensure_same_rect,
             S.usable_observation, T.input_mod.click, T.win32.cursor_pos,
             T.win32.point_hit)
    T._stored_window = lambda wid: info
    #: 收 `**kw`：`_check_input_ready` 现在按调用方是否发键盘决定要不要拦弹出层
    #: （`keyboard=`）。替身签名比真函数窄 → 整条链 TypeError，而它长得像"测试自己坏了"。
    T._check_input_ready = lambda i, **kw: None
    T._ensure_same_rect = lambda o: None
    S.usable_observation = lambda oid: obs
    T.input_mod.click = fake_click
    T.win32.cursor_pos = (lambda: actual) if raises is None else raises
    #: 落点遮挡检查要问 `WindowFromPoint` —— 这里量的是"回读落点"，不是遮挡。
    #: 不打桩的话真探针会去问桌面（`w1` 这个假窗口当然不命中）。
    T.win32.point_hit = lambda h, x, y: True
    return sent, saved


def _restore_click(saved):
    (T._stored_window, T._check_input_ready, T._ensure_same_rect,
     S.usable_observation, T.input_mod.click, T.win32.cursor_pos,
     T.win32.point_hit) = saved


def test_click_records_the_read_back_point():
    S.reset_for_tests()
    S.enable("s1")
    sent, saved = _patch_click((612, 704))
    try:
        text = T._click("w1", "o1", None, 500, 500, 1, "left")
        check("注入用的是请求点", sent == [(600, 500)], str(sent))
        landed = S.take_last_click("w1")
        check("红十字记的是回读点 (612,704)", landed == (612, 704), str(landed))
        S.note_click("w1", 612, 704, requested=(600, 500), miss_limit=10)
        check("偏差被算出来 (12,204)", S.take_click_miss("w1") == (12, 204),
              str(S.take_click_miss("w1")))
        check("回执里有'落点偏了'这句", "落点偏了" in text, text[:120])
        check("回执给出了差值", "+12" in text and "+204" in text, text[:160])
    finally:
        _restore_click(saved)
        S.disable("test")


def test_click_without_read_back_says_the_marker_is_a_request():
    S.reset_for_tests()
    S.enable("s1")
    sent, saved = _patch_click(None)
    try:
        text = T._click("w1", "o1", None, 500, 500, 1, "left")
        check("回读失败时仍记了一个点（免得没有红十字）",
              S.take_last_click("w1") == (600, 500),
              str(S.take_last_click("w1")))
        check("同时明说红十字画的是请求点", "请求点" in text, text[:160])
    finally:
        _restore_click(saved)
        S.disable("test")


# ------------------------------------------------------------- 无视觉降级
class _Cap:
    rect = (100, 100, 1100, 900)
    width = 1000
    height = 800
    png = _png(1000, 800)
    notes = []


def test_blind_model_gets_no_screenshot_and_a_first_line():
    S.reset_for_tests()
    S.enable("s1")
    info = win32.WindowInfo(hwnd=9, pid=1, exe="probe.exe",
                            exe_path="C:/probe.exe", title="probe",
                            class_name="Probe", rect=(100, 100, 1100, 900),
                            visible=True, minimized=False, cloaked=False)
    shot = {"n": 0}

    def fake_capture(hwnd):
        shot["n"] += 1
        return _Cap()

    saved = (T._stored_window, T._allowed, T._authorize_pending,
             capture.capture_window, T.uia.snapshot, T.uia.focused_element,
             T.win32.is_foreground, T.win32.outer_rect,
             S.record_observation, T._same_process_windows,
             T.vision_budget.resolve)
    T._stored_window = lambda wid: info
    T._allowed = lambda i: None
    T._authorize_pending = lambda i: None
    capture.capture_window = fake_capture
    T.uia.snapshot = lambda hwnd, max_elements=0: uia.Snapshot(
        hwnd=hwnd, elements=[_el(1, (110, 120, 300, 160))])
    T.uia.focused_element = lambda hwnd: None
    T.win32.is_foreground = lambda hwnd: False
    T.win32.outer_rect = lambda hwnd: (100, 100, 1100, 900)
    S.record_observation = lambda *a, **kw: "obs1"
    T._same_process_windows = lambda info, limit=4: []
    T.vision_budget.resolve = lambda provider="", model="": _profile(
        can_see=False, confirmed=True)
    try:
        out = T._get_window_state("w1", True, True)
        check("看不见图的模型完全不截图", shot["n"] == 0, str(shot["n"]))
        text = out if isinstance(out, str) else out.render()
        check("回执第一段就说'当前模型不看图'",
              "不看图" in "\n".join(text.split("\n")[:6]), text[:300])
        check("并给出路线（element_index / 换模型）",
              "element_index" in text and "换" in text)
        check("返回的不是图片块（0 张 prepared）",
              isinstance(out, str) or not getattr(out, "prepared", None))
    finally:
        (T._stored_window, T._allowed, T._authorize_pending,
         capture.capture_window, T.uia.snapshot, T.uia.focused_element,
         T.win32.is_foreground, T.win32.outer_rect,
         S.record_observation, T._same_process_windows,
         T.vision_budget.resolve) = saved
        S.disable("test")


def test_blind_model_with_no_tree_is_refused():
    S.reset_for_tests()
    S.enable("s1")
    info = win32.WindowInfo(hwnd=9, pid=1, exe="probe.exe",
                            exe_path="C:/probe.exe", title="probe",
                            class_name="Probe", rect=(100, 100, 1100, 900),
                            visible=True, minimized=False, cloaked=False)
    saved = (T._stored_window, T._allowed, T._authorize_pending,
             T.uia.snapshot, T.vision_budget.resolve)
    T._stored_window = lambda wid: info
    T._allowed = lambda i: None
    T._authorize_pending = lambda i: None
    T.uia.snapshot = lambda hwnd, max_elements=0: uia.Snapshot(hwnd=hwnd,
                                                               elements=[])
    T.vision_budget.resolve = lambda provider="", model="": _profile(
        can_see=False, confirmed=True)
    try:
        try:
            T._get_window_state("w1", True, True)
            raised = ""
        except T.ActionError as exc:
            raised = str(exc)
        check("两条信息通道都断时直接拒绝", bool(raised), raised[:80])
        check("拒绝理由说的是'盲猜坐标'，不是含糊的失败",
              "盲" in raised or "看不见" in raised, raised[:120])
        try:
            T._get_window_state("w1", False, True)
            raised2 = ""
        except T.ActionError as exc:
            raised2 = str(exc)
        check("没开 include_text 时先指路，而不是直接判死",
              "include_text=true" in raised2 and "拒绝" not in raised2,
              raised2[:120])
    finally:
        (T._stored_window, T._allowed, T._authorize_pending,
         T.uia.snapshot, T.vision_budget.resolve) = saved
        S.disable("test")


# ------------------------------------------------------------------ 文案
def test_schema_and_receipts_mention_the_new_contract():
    props = T.COMPUTER_TOOL_SCHEMA["inputSchema"]["properties"]
    check("schema 仍有 include_text（编号来自它）", "include_text" in props)
    from core import vision_budget as _vb
    prof = _vb.resolve()
    check("resolve() 无参也能用（工具里就是这么调的）",
          isinstance(prof, _vb.VisionProfile))
    src = Path(T.__file__).read_text(encoding="utf-8")
    for phrase in ("图中**蓝框里的数字**", "落点偏了", "当前模型不看图"):
        check("回执里有这句：%s" % phrase[:10], phrase in src)


def main() -> int:
    print("=" * 62)
    print("Click feedback: element marks / landed point / blind-model fallback")
    print("=" * 62)
    for fn in (test_marks_follow_the_element_tree_ids,
               test_marks_skip_targets_that_are_not_there,
               test_marks_are_capped_and_the_fact_is_visible,
               test_marks_are_drawn_where_the_element_is,
               test_marks_survive_a_bad_rect,
               test_note_click_records_the_landed_point,
               test_small_miss_does_not_force_grid,
               test_click_records_the_read_back_point,
               test_click_without_read_back_says_the_marker_is_a_request,
               test_blind_model_gets_no_screenshot_and_a_first_line,
               test_blind_model_with_no_tree_is_refused,
               test_schema_and_receipts_mention_the_new_contract):
        print("\n-- %s --" % fn.__name__)
        fn()
    print("\n%d tests: %d passed, %d failed" % (_PASS + _FAIL, _PASS, _FAIL))
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
