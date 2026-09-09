import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * 只包住画图那一层的错误边界。
 *
 * 没有它的话，`new Sigma(...)` 在 WebGL 被禁用的机器上抛出来（企业镜像里
 * 很常见），或者发版后老标签页懒加载 chunk 404，React 会把整棵树卸载掉：
 * 用户看到的是**一整页空白**——数据已经取到了、截断提示已经算好了，全都
 * 跟着一起消失，连换一个实体重搜的输入框都没了。
 *
 * 边界只包渲染层，取数、截断提示、搜索框都在它外面——所以画不出来的时候
 * 那些信息还在，用户能看见原因，也能接着干别的。
 */
export class GraphErrorBoundary extends Component<
  { children: ReactNode; fallback: (retry: () => void) => ReactNode },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 打到控制台：文案只说"画不出来"，具体是哪一行抛的只有这里留得下。
    console.error('邻域图渲染失败', error, info.componentStack)
  }

  private retry = () => this.setState({ failed: false })

  render() {
    if (this.state.failed) return this.props.fallback(this.retry)
    return this.props.children
  }
}
