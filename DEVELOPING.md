# Developing baraza

For anyone reading the source, and for whoever maintains it upstream. Pull requests are
not accepted in the generated repository (see the README), so this describes how the
package is built and how it is checked, rather than a contribution workflow.

## Setup

```bash
pip install -e ".[dev]"
pytest
```

That is enough for everything except the browser walk, which needs the front end
compiled and a browser installed:

```bash
cd ui && npm ci && npm run build   # writes into ../src/baraza/web/static/
cd ..
pip install -e ".[walkthrough]"
playwright install chromium
pytest walkthrough
```

The compiled UI is **not committed**. A fresh checkout has no `web/static/`, and that is
the normal state. `baraza.web.ui` serves a page saying so rather than a bare 404, so being
in a source tree does not look like a broken install.

## Layout

```
cli.py            four commands: serve, import, report, mcp
importer.py       Luma -> the store. The free folder path and the paid API path.
credentials.py    the Luma key and session token on disk

ingest/           reading Luma: the API client, the guest-list CSV parser,
                  the event resolver, and the two shared mappings
store/            one SQLite file. Facts only, never conclusions.
analytics/        every number, as pure functions over (events, attendances)
web/              the JSON API, the loopback server, the token, the static UI
mcp/              a read-only MCP surface. tools.py imports no protocol.
ui/               the React front end, compiled into the wheel at release
```

Four boundaries hold this together.

### Analytics is a function of its arguments

Nothing in `analytics/` reads a clock, a file or an environment variable. `now` is passed
in. The same folder of exports scored twice gives the same answer, and a test can ask
what the numbers were on any date without mocking anything.

This is why `now` is an *operator* parameter on the MCP tools rather than a client one: a
model that could set it could ask what the community looked like on a date of its
choosing, and then report numbers that disagree with the screens.

### The store holds facts, not conclusions

Scores and lifecycle labels are computed on read, never written. Change a threshold and
the roster re-labels itself immediately, with no cache to invalidate and no stale label
to explain. It is also what lets a stored calendar be re-scored later without going back
to the CSVs.

### `mcp/tools.py` imports no protocol

Everything an MCP client can actually get is a plain function over the store, tested
without standing up a server. `mcp/server.py` is a thin registration shim, so the half
that can be wrong is tested without the optional dependency installed.

### The server binds loopback, and there is no parameter to change it

There is no login, because nothing outside the machine can reach the socket. A host
argument would have exactly one safe value, and somebody eventually widens a parameter like
that. A test asserts the absence, sweeping everything the module publishes rather than a
written-down list of names.

## Tests

| | What it covers | Needs |
|---|---|---|
| `pytest` | every number, every endpoint, the CLI, the store | nothing |
| `pytest walkthrough` | what only a browser sees | a built UI + chromium |
| CI's `baraza` job | the same suite with **only declared dependencies installed** | Linux |

The third one is why there is a separate job: the package declares its dependencies, and
installing only those proves the declaration. A development checkout has more on the path
and would never notice a stray import.

The browser walk covers what an endpoint test cannot see: a screen reachable once and
never again, a form that erases the list it belongs to, a link that blanks the page. In
every one of those the endpoints answer correctly throughout.

## What not to break

**The UI is compiled into the wheel.** If `ui/` does not build, the release ships a
package that serves a "not built" page to every user. CI builds it in a separate job and
asserts an `index.html` came out.

**`csv.field_size_limit` is process-global.** The guest-list parser raises it so that one
oversized cell cannot cost a whole file, and holds a lock while it does, because two
concurrent parses otherwise restore each other's values and leave it raised for the whole
process, including for a host application that imported the parser. If you touch that
code, keep the lock.

**PRAGMA statements are interpolated, and have to be.** SQLite does not accept bound
parameters there, and both forms are a syntax error. The values are module constants or a
table name read back from `sqlite_master`, never anything from data.

**Some fallbacks that look unreachable are required.** The UI compiles with
`noUncheckedIndexedAccess`, so indexing a `Record` yields `string | undefined` and the
`??` is not optional padding. Deleting one fails the build.

**`INK_DARK` and `INK_WHITE` have no TypeScript importer**, and are exported anyway:
`tests/test_palette_contrast.py` reads them out of the source so the contrast check
scores against the ink the app really uses rather than a second copy of the values. A
dead-code sweep that only looks at TypeScript callers will find them unused and be wrong.

## Releasing

1. `cd ui && npm ci && npm run build`, so the wheel carries the compiled UI.
2. `pytest && pytest walkthrough && ruff check . && mypy src`
3. Build and open the wheel before uploading. It should contain `baraza/` and nothing
   else: no store, no token file, no test data.
4. `requires-python` is a promise. CI runs the floor and the ceiling as separate jobs,
   because a floor nobody executes is only a string: a package declaring a version it
   cannot run installs cleanly and fails on first use.
