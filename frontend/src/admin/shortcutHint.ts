/**
 * 快捷键提示上写哪个修饰键。
 *
 * 监听那边一直是 `event.metaKey || event.ctrlKey`，两个平台都能按开；
 * 错的只是提示文案——它写死了 ⌘K，Windows 用户照着按不出来，然后得出
 * 「这个功能坏了」的结论。提示比功能更容易骗人，因为没人会怀疑它。
 *
 * 判不出平台就说 Ctrl：Mac 用户按 Ctrl+K 一样能打开（监听认两个键），
 * 反过来 Windows 用户按 ⌘ 却无键可按。猜错的代价不对称。
 */
export function shortcutModifierLabel(): string {
  const platform =
    (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ??
    navigator.platform ??
    ''
  return /mac|iphone|ipad|ipod/i.test(platform) ? '⌘' : 'Ctrl+'
}

/** 命令面板的快捷键提示，形如 `Ctrl+K` / `⌘K`。 */
export function commandPaletteHint(): string {
  return `${shortcutModifierLabel()}K`
}
