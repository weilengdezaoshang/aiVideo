import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { EvaluationApp } from './EvaluationApp.js'

const rootElement = document.getElementById('evaluation-root')
if (rootElement) {
  createRoot(rootElement).render(
    <StrictMode>
      <EvaluationApp />
    </StrictMode>,
  )
}
