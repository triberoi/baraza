# The walkthrough harness

A browser walks the five screens of `baraza serve` against a temporary store.

It is deliberately small: no credentials, no remote fixtures, nothing to roll back. It
starts the app the way `baraza serve` does, on a store it has just built, and clicks.

## Why it lives outside `tests/`

`pyproject.toml` sets `testpaths = ["tests"]`, so an ordinary `pytest` never collects
this. That is the point: it needs a **built** front end and a browser binary, and neither
is present in a plain checkout or in the job that installs only baraza's declared
dependencies.

It is not skipped into nothing. The `walkthrough` CI job exists to run it, builds the UI
first, and installs the browser.

## Running it locally

```bash
cd ui && npm ci && npm run build   # the app has to exist to be walked
cd ..
pip install -e ".[walkthrough]"
playwright install chromium
pytest walkthrough
```

`pytest walkthrough --headed --slowmo 300` to watch it.
