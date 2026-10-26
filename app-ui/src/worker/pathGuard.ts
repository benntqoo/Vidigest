// 目录归属判断 —— 只用于「不能把工作/模型目录放进安装目录」这一条。
//
// 为什么需要自己写：`child.startsWith(parent)` 会把
// `C:\Program Files\Vidigest-old` 判成在 `C:\Program Files\Vidigest` 里面，
// 用户明明选了合法目录却被拒；而只拼分隔符再比又会漏掉「刚好相等」这一种。
// 所以先比相等、再比「父目录 + 分隔符」前缀，并且按 Windows 语义忽略大小写。
//
// ⚠️ 后端 `app/core/paths.py::dir_is_inside` 是**同一条规则的硬拦**。
// 这里只是为了「边输边提示」，不是安全边界 —— 用户改配置文件就能绕过。

function normalize(p: string): string {
  // 统一分隔符、去掉尾部分隔符、Windows 下大小写不敏感。
  const unified = p.trim().replace(/\//g, "\\").replace(/\\+$/, "");
  return unified.toLowerCase();
}

/**
 * `child` 是否等于 `parent` 或位于其下。
 *
 * `parent` 为 `null`（源码运行，没有安装目录概念）时**永远返回 false**。
 */
export function dirIsInside(child: string, parent: string | null | undefined): boolean {
  if (!parent || !child.trim()) return false;
  const a = normalize(child);
  const b = normalize(parent);
  if (!b) return false;
  return a === b || a.startsWith(`${b}\\`);
}
