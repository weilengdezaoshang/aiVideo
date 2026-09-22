import type { AssetEntry } from '../../workspace/api.js'
import type { Reference } from '../state/node-model.js'

/** Asset selection never inserts another object into the canvas. */
export function pickReferenceAsset(signal: AbortSignal): Promise<Reference | null> {
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog')
    dialog.className = 'reference-asset-dialog'
    dialog.innerHTML =
      '<header><h2>从资产选择参考图</h2><button type="button">取消</button></header><div class="reference-asset-grid" role="list" aria-label="图片资产"></div><p role="status">正在加载资产…</p>'
    const grid = dialog.querySelector<HTMLDivElement>('.reference-asset-grid')!
    const status = dialog.querySelector('p')!
    const finish = (value: Reference | null) => {
      signal.removeEventListener('abort', cancel)
      dialog.close()
      dialog.remove()
      resolve(value)
    }
    const cancel = () => finish(null)
    dialog.querySelector('button')!.onclick = cancel
    dialog.addEventListener('cancel', (e) => {
      e.preventDefault()
      cancel()
    })
    signal.addEventListener('abort', cancel, { once: true })
    document.body.append(dialog)
    dialog.showModal()
    void fetch('/api/assets?kind=image&limit=500', { signal })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error('资产加载失败，请关闭后重试')
        }
        const body = (await response.json()) as { assets: AssetEntry[] }
        if (signal.aborted || !dialog.isConnected) {
          return
        }
        status.textContent = body.assets.length ? '' : '暂无图片资产，请先上传或生成图片'
        for (const { asset, urls } of body.assets) {
          const button = document.createElement('button')
          button.type = 'button'
          const name = asset.name || '未命名图片'
          const image = document.createElement('img')
          image.src = urls.thumb256 || urls.original
          image.alt = name
          image.loading = 'lazy'
          button.append(image, document.createTextNode(name))
          button.onclick = () => finish({ assetId: asset.id, ext: asset.ext, name })
          grid.append(button)
        }
      })
      .catch((error) => {
        if (!signal.aborted) {
          status.textContent = error instanceof Error ? error.message : '资产加载失败'
        }
      })
  })
}
