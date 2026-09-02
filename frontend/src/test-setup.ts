import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/react'

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


// 前台聊天窗滚到底之前会问一句 prefers-reduced-motion，jsdom 里没有
// matchMedia。默认答"不减少动效"——这是绝大多数用户的真实设置，测试里
// 也就走到平时走的那条分支。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

// waitFor / findBy* 的默认超时是 1s。全量并发跑的时候，懒加载的命令面板
// chunk 和整棵树的重渲染经常超过它——单独跑绿、全量跑红，而报出来的是
// "找不到元素"，指向的地方跟真正的原因毫无关系。
configure({ asyncUtilTimeout: 5000 })
