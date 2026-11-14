// 说明页正文里出现的数字——只此一处。
//
// 为什么单独一个文件：正文（`docs/help/*.md`）里写的是
//    `{breaker_consecutive}` 这类占位，渲染时才替换。手写"3 次 / 20 次 / 120 秒"
//    会与后端常量各漂各的，而文案漂了没有任何东西会红。
//    读者：`tests/test_help_docs.py`（按 AST 读这个文件，与
//    `core/auto_review.DENIAL_MAX_*` / `core/approval_timeout.DEFAULT_SECONDS` 比）。
//
// `approval_timeout_seconds` 是默认值，用户在设置页可以改（0 = 一直等）。
//    正文里那句已经写清"可以在设置页改"。

export const HELP_NUMBERS = {
  breaker_consecutive: 5,
  breaker_total: 20,
  approval_timeout_seconds: 120,
};
