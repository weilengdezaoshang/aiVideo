export function nextMenuIndex(disabled: readonly boolean[], current: number, key: string): number {
  if (!disabled.length || disabled.every(Boolean)) {
    return -1
  }
  const direction = key === 'ArrowUp' || key === 'End' ? -1 : 1
  let index =
    key === 'Home'
      ? 0
      : key === 'End'
        ? disabled.length - 1
        : current < 0
          ? direction > 0
            ? 0
            : disabled.length - 1
          : (current + direction + disabled.length) % disabled.length
  while (disabled[index]) {
    index = (index + direction + disabled.length) % disabled.length
  }
  return index
}
