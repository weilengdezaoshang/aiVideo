/** Keep browser page zoom from competing with the mounted canvas's gestures. */
export function preventBrowserGestureZoom(document: Document): () => void {
  const preventWheelZoom = (event: WheelEvent) => {
    if (event.ctrlKey) {
      event.preventDefault()
    }
  }
  const preventGestureZoom = (event: Event) => event.preventDefault()
  const options: AddEventListenerOptions = { passive: false, capture: true }
  document.addEventListener('wheel', preventWheelZoom, options)
  const gestureEvents = ['gesturestart', 'gesturechange', 'gestureend'] as const
  for (const type of gestureEvents) {
    document.addEventListener(type, preventGestureZoom, options)
  }
  return () => {
    document.removeEventListener('wheel', preventWheelZoom, options)
    for (const type of gestureEvents) {
      document.removeEventListener(type, preventGestureZoom, options)
    }
  }
}
