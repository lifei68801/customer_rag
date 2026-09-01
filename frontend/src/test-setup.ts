/**
 * jsdom 缺的浏览器 API。
 *
 * cmdk（⌘K 面板）在挂载时就 new ResizeObserver，jsdom 里没有这个类，
 * 面板会直接崩掉——而且崩在 React 的 passive effect 里，报出来的是
 * "找不到元素"，不是"ResizeObserver is not defined"。
 */
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (!('ResizeObserver' in globalThis)) {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver
}

// 同理：cmdk 高亮选中项时会 scrollIntoView，jsdom 的 Element 上没有这个
// 方法。它只影响滚动位置，测试里什么都不用做。
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {}
}
