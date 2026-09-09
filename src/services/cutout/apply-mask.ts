import sharp from 'sharp'

/** 把灰度 Mask 写入原图 Alpha；白色保留、黑色透明。 */
export async function applyCutoutMask(original: Buffer, mask: Buffer): Promise<Buffer> {
  const originalMeta = await sharp(original, { failOn: 'error' }).metadata()
  const maskMeta = await sharp(mask, { failOn: 'error' }).metadata()
  if (!originalMeta.width || !originalMeta.height) {
    throw new Error('无法读取原图尺寸')
  }
  if (maskMeta.width !== originalMeta.width || maskMeta.height !== originalMeta.height) {
    throw new Error('Mask 尺寸必须与原图一致')
  }
  const rgb = await sharp(original).removeAlpha().raw().toBuffer()
  const alpha = await sharp(mask).removeAlpha().greyscale().raw().toBuffer()
  const rgba = Buffer.alloc(originalMeta.width * originalMeta.height * 4)
  for (let pixel = 0; pixel < originalMeta.width * originalMeta.height; pixel++) {
    rgba[pixel * 4] = rgb[pixel * 3]
    rgba[pixel * 4 + 1] = rgb[pixel * 3 + 1]
    rgba[pixel * 4 + 2] = rgb[pixel * 3 + 2]
    rgba[pixel * 4 + 3] = alpha[pixel]
  }
  return sharp(rgba, {
    raw: { width: originalMeta.width, height: originalMeta.height, channels: 4 },
  })
    .png()
    .toBuffer()
}
