import { installSessionFetch } from '../shared/session-fetch.js'
installSessionFetch()

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { LandingPage } from './LandingPage.js'

const root = document.getElementById('landing-root')
if (root) {
  createRoot(root).render(
    <StrictMode>
      <LandingPage />
    </StrictMode>,
  )
}
