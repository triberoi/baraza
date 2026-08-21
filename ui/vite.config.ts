import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The build lands *inside the Python package*, at baraza/web/static, because that is
// what makes `pip install baraza` enough on its own — the organizer needs Python and
// nothing else. The directory is gitignored: assets are built at
// release, not committed, so the repo never carries build output that can disagree
// with its source.
//
// `base: '/'` — asset URLs are absolute from the root, and that is load-bearing.
//
// It was `'./'` first, on the reasoning that an ephemeral port means no origin to
// hard-code. That confused the *origin* with the *path*: a relative `./assets/x.js` is
// resolved against the current URL, so on `/people/mem_0001` the browser asks for
// `/people/assets/x.js`. The single-page fallback then answers **200 with index.html**,
// because it answers every unknown path that way — so the browser received HTML where
// it expected a module, the script never parsed, and the screen went silently blank.
// `/` and `/people` worked; only the nested route broke. Found by walking it.
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../src/baraza/web/static',
    emptyOutDir: true,
    // Fonts are bundled rather than fetched: a tool that phones fonts.googleapis.com
    // on every load does not work on a plane and tells Google who is running it.
    // Inlining is capped so the woff2 files stay separate, cacheable files.
    assetsInlineLimit: 4096,
  },
  server: {
    // Dev-server only, and loopback for the same reason the Python server is — see
    // baraza/web/server.py. `vite dev` proxies the API to a `baraza serve` on 8000.
    host: '127.0.0.1',
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
})
