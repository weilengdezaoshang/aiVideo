// jsdom 测试环境:为 React 组件与 DOM 依赖模块安装浏览器全局。
// node --test 每个文件独立进程,安装一次即可;jsdom 未实现的 API
// (matchMedia / WAAPI / scrollIntoView / img.decode)给出最小桩实现。
import { JSDOM } from 'jsdom'

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'https://localhost/',
  pretendToBeVisual: true,
})
const { window } = dom

const globals = globalThis as unknown as Record<string, unknown>
Object.assign(globals, {
  window,
  document: window.document,
  location: window.location,
  history: window.history,
  Image: window.Image,
  HTMLElement: window.HTMLElement,
  HTMLInputElement: window.HTMLInputElement,
  HTMLSelectElement: window.HTMLSelectElement,
  HTMLTextAreaElement: window.HTMLTextAreaElement,
  HTMLImageElement: window.HTMLImageElement,
  HTMLMediaElement: window.HTMLMediaElement,
  Element: window.Element,
  Node: window.Node,
  SVGElement: window.SVGElement,
  DocumentFragment: window.DocumentFragment,
  CustomEvent: window.CustomEvent,
  Event: window.Event,
  KeyboardEvent: window.KeyboardEvent,
  MouseEvent: window.MouseEvent,
  PointerEvent: window.PointerEvent ?? window.Event,
  DragEvent: window.DragEvent ?? window.Event,
  getComputedStyle: window.getComputedStyle.bind(window),
  requestAnimationFrame: window.requestAnimationFrame.bind(window),
  cancelAnimationFrame: window.cancelAnimationFrame.bind(window),
  localStorage: window.localStorage,
  customElements: window.customElements,
  // React 19 act() 环境标记
  IS_REACT_ACT_ENVIRONMENT: true,
})

// jsdom 未实现 matchMedia:默认"无动效偏好",测试可按需覆写 matches
window.matchMedia = ((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
  dispatchEvent: () => false,
})) as unknown as typeof window.matchMedia

// WAAPI / 滚动 / 图片解码桩
window.Element.prototype.animate = (() => ({
  cancel() {},
  finish() {},
  onfinish: null,
})) as unknown as typeof window.Element.prototype.animate
window.Element.prototype.scrollIntoView = () => {}
window.HTMLImageElement.prototype.decode = () => Promise.resolve()

export { window as testWindow, dom as testDom }
