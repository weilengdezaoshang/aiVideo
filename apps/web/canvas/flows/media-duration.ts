export function videoDuration(url: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const media = document.createElement('video')
    const done = (error?: string) => {
      clearTimeout(timer)
      const duration = media.duration
      media.onloadedmetadata = media.onerror = null
      media.removeAttribute('src')
      media.load()
      if (error || !Number.isFinite(duration) || duration <= 0 || duration > 3600) {
        reject(new Error(error || '视频时长无效，单段支持最长 1 小时'))
      } else {
        resolve(Math.max(1, Math.floor(duration * 30)))
      }
    }
    const timer = setTimeout(() => done('读取视频超时，请检查素材后重试'), 15000)
    media.onloadedmetadata = () => done()
    media.onerror = () => done('无法读取视频，请使用浏览器支持的真实视频文件')
    media.preload = 'metadata'
    media.src = url
  })
}
