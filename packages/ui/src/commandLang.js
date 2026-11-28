// 命令行代码块的语言标注（纯函数，node 可直接测）。
//
// 用途只有一个：在代码块左上角给「人」看一眼这段该在哪个 shell 跑，
// 免得协作时把 PowerShell 贴进 Bash。它不参与命令能否执行、风险分级、
// 提示词或任何翻译 —— 那些由后端按真实 shell 判（core/shell.py::shell_kind）。
//
// 判据是 DNA 级的：只认「语法上另一边不可能出现」的特征。
// 宁可漏判，不可误判 —— 标签说错一次，人就敢把命令贴错地方，
// 那比不标更坏。凡是「看着像、但另一边也能写出」的一律不收：
//   · && / ||      —— PowerShell 7 也支持
//   · grep/curl/find —— PowerShell 里能直接调这些 exe
//   · C:\ 盘符路径  —— Git Bash 在 Windows 上同样写得出
//   · $var          —— 两边都用
//   · 2>/dev/null   —— PS Core on Linux 也有 /dev/null
//   · 反引号         —— Bash 里是命令替换，两边都有意义
// 围栏已经写了语言时直接用那个 token，不会进这里。

// PowerShell 的 DNA：这些写法在 Bash 语法里不可能成立。
const PS_DNA = [
  // .NET 静态成员访问 —— Bash 里 `[` 是 test 内建，`::` 不是有效语法
  /\[[A-Za-z][A-Za-z0-9.]*\]::[A-Za-z_]/,
  // 作用域变量 $env: / $global: / $script: / $local: —— Bash 的 $env 后面跟
  // ":VAR" 是字面量，不是变量；这个形状只在 PowerShell 里是变量引用
  /\$(?:env|global|script|local|private|using):[A-Za-z_]/,
  // cmdlet 的 动词-名词 命名约定（连字符 + 大写名词词首）。
  // Bash 的命令名约定不含这种形式；要用得人为定义同名函数，不是自然写法。
  /\b(?:Get|Set|New|Remove|Select|Where|ForEach|Test|Add|Clear|Import|Export|Write|Read|Start|Stop|Restart|Move|Copy|Rename|Join|Split|Sort|Group|Format|Out|Invoke|Resolve|Confirm|Update|Disable|Enable|Install|Uninstall|Find|Save)-[A-Z][A-Za-z]+\b/,
];

// Bash / POSIX shell 的 DNA：这些是 shell 保留字与语法结构。
const SH_DNA = [
  /\bthen\b/,                                       // if ... then
  /\bfi\b/,                                         // if 的收尾
  /\bdone\b/,                                       // for/while 的收尾
  /\bexport\s+[A-Za-z_][A-Za-z0-9_]*=/,             // POSIX export 赋值
  /\bset\s+-[a-z]+\b/,                             // set -e / set -o pipefail
  /<<-?\s*['"]?[A-Za-z_]/,                          // heredoc（PS 是 @"..."@）
  /\[\[[^\]]+\]\]/,                                 // bash 的条件测试 [[ ]]
];

function any(patterns, text) {
  return patterns.some(re => re.test(text));
}

/**
 * 没标语言的命令文本 → "powershell" / "bash" / ""（没把握就不猜）。
 *
 * 只有一边出现 DNA 特征才判；两边都出现（对照/混贴）或都没有返回空。
 * 不做「看起来像命令」的形状推断 —— DNA 特征自身就是证明：
 * .NET 静态访问、cmdlet、then/fi/export 不可能长在赋值语句里。
 */
export function detectCommandLanguage(text) {
  const s = String(text == null ? "" : text);
  if (!s.trim()) return "";
  const ps = any(PS_DNA, s);
  const sh = any(SH_DNA, s);
  if (ps && !sh) return "powershell";
  if (sh && !ps) return "bash";
  return "";
}

/** 标签上给人看的写法（不参与任何逻辑）。 */
export function languageLabel(lang) {
  const s = String(lang || "").toLowerCase();
  if (s === "powershell" || s === "pwsh" || s === "ps1") return "PowerShell";
  if (s === "bash" || s === "sh" || s === "shell" || s === "zsh") return "Bash";
  if (s === "cmd" || s === "bat") return "CMD";
  return s;
}
