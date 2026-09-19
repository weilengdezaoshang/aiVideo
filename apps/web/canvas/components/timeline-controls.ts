/** Shared DOM controls for the embedded and expanded timeline. */
export function timelineButton(label: string, action: () => void): HTMLButtonElement {
  const button = document.createElement('button')
  button.type = 'button'
  button.textContent = label
  button.addEventListener('click', action)
  return button
}

export function timelineTime(frame: number): string {
  const seconds = Math.floor(frame / 30)
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}
