// Fonts before tokens. `fonts.css` declares the three brand faces from bundled
// woff2 files rather than fetching them from fonts.googleapis.com — a tool running
// on someone's laptop must not report every launch to a third party, and must work
// on a plane. It is hand-written rather than @fontsource's own CSS because that
// ships a legacy `woff` beside every `woff2` and every subset of every face; see
// that file for the two rules that cut the build to a third of its size.
import './fonts.css'

import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from './App'
import { claimToken } from './api'
import './tokens.css'

// Before the first render, so no component ever sees the token in the URL and the
// address bar is clean by the time anyone could screenshot it.
const authorized = claimToken()

const root = document.getElementById('root')
if (!root) throw new Error('no #root in the page — the HTML shell and this script disagree')

createRoot(root).render(
  <StrictMode>
    <App authorized={authorized} />
  </StrictMode>,
)
