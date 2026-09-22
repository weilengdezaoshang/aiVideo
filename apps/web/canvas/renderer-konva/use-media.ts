import { useEffect, useState } from 'react'

export function useImageElement(src: string | null): HTMLImageElement | null {
  const [image, setImage] = useState<HTMLImageElement | null>(null)
  useEffect(() => {
    if (!src) {
      setImage(null)
      return
    }
    const next = new Image()
    let active = true
    next.decoding = 'async'
    next.onload = () => active && setImage(next)
    next.onerror = () => active && setImage(null)
    next.src = src
    return () => {
      active = false
      next.onload = null
      next.onerror = null
    }
  }, [src])
  return image
}

export function useVideoElement(src: string | null): HTMLVideoElement | null {
  const [video, setVideo] = useState<HTMLVideoElement | null>(null)
  useEffect(() => {
    if (!src) {
      setVideo(null)
      return
    }
    const next = document.createElement('video')
    next.src = src
    next.muted = true
    next.loop = true
    next.playsInline = true
    next.preload = 'metadata'
    const ready = () => {
      setVideo(next)
      void next.play().catch(() => {})
    }
    next.addEventListener('loadeddata', ready, { once: true })
    return () => {
      next.pause()
      next.removeAttribute('src')
      next.load()
      setVideo((current) => (current === next ? null : current))
    }
  }, [src])
  return video
}
