/** How a bitmap sits in a frame. Layered posters use contain; older images stay cover. */

export type FrameBox = { x: number; y: number; width: number; height: number }

export function containFrame(
  sourceWidth: number,
  sourceHeight: number,
  frameWidth: number,
  frameHeight: number,
): FrameBox {
  const scale = Math.min(frameWidth / sourceWidth, frameHeight / sourceHeight)
  const width = sourceWidth * scale
  const height = sourceHeight * scale
  return {
    x: (frameWidth - width) / 2,
    y: (frameHeight - height) / 2,
    width,
    height,
  }
}

export function coverCrop(
  sourceWidth: number,
  sourceHeight: number,
  frameWidth: number,
  frameHeight: number,
): FrameBox {
  const sourceRatio = sourceWidth / sourceHeight
  const targetRatio = frameWidth / frameHeight
  if (sourceRatio > targetRatio) {
    const cropWidth = sourceHeight * targetRatio
    return { x: (sourceWidth - cropWidth) / 2, y: 0, width: cropWidth, height: sourceHeight }
  }
  const cropHeight = sourceWidth / targetRatio
  return { x: 0, y: (sourceHeight - cropHeight) / 2, width: sourceWidth, height: cropHeight }
}
