/**
 * The shell and the router.
 *
 * One rule runs through the loading here: **a screen with no data yet is not the same
 * as a screen that failed**, and neither is the same as a store with nothing in it.
 * Collapsing those three is how a local tool ends up showing a spinner forever or an
 * error for an empty calendar an organizer has simply not filled yet.
 *
 * Data is fetched per screen rather than once at the top, because every endpoint
 * recomputes from the store on every request (nothing is cached server-side, by
 * design). That is what makes a threshold change on the Settings screen visible on the
 * very next screen an organizer opens, with no cache to invalidate.
 */
import { useCallback, useEffect, useState } from 'react'
// The on-dark lockup: the bar is navy. Letterforms are outlined in the file, so the
// wordmark is identical on a machine with no Montserrat installed.
import wordmark from './assets/logo-on-dark.svg'

import { ApiError, get, type Overview as OverviewData, type Person, type Retention, type Status, type TimelineEntry } from './api'
import { Link, useRoute, type Route } from './router'
import { ImportScreen } from './screens/ImportScreen'
import { Overview } from './screens/Overview'
import { People } from './screens/People'
import { PersonScreen } from './screens/PersonScreen'
import { Returning } from './screens/Returning'
import { SettingsScreen } from './screens/SettingsScreen'
import './app.css'

// `status` is the HTTP code when there was a response at all, and undefined when the
// request never got one. Both halves matter: decide "your session ended" from the code,
// never by pattern-matching the message, or a store path containing "Sessions" reads as
// an expired token.
type Load<T> =
  | { state: 'loading' }
  | { state: 'ready'; data: T }
  | { state: 'failed'; message: string; status?: number }

/** One fetch, three outcomes, and a `refresh` the caller can pull after a write. */
function useLoad<T>(path: string | null, deps: unknown[] = []): [Load<T>, () => void] {
  const [load, setLoad] = useState<Load<T>>({ state: 'loading' })
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    if (path === null) return
    let live = true
    // A REFRESH keeps showing what it already has. Dropping straight back to `loading`
    // unmounted whatever the caller was rendering, which is how naming one pending event
    // erased the import report, the skipped-file list and every event still waiting for a
    // name. The first load still shows the spinner — there is nothing
    // else to show — and only that one does.
    setLoad((prev) => (prev.state === 'ready' ? prev : { state: 'loading' }))
    get<T>(path)
      .then((data) => live && setLoad({ state: 'ready', data }))
      .catch(
        (error: unknown) => {
          if (!live) return
          // A non-`ApiError` means the request never reached a server, usually because the
          // organizer closed the terminal running `baraza serve`. It needs a sentence of
          // its own: `String(error)` puts `TypeError: Failed to fetch` on screen.
          if (error instanceof ApiError) {
            setLoad({ state: 'failed', message: error.message, status: error.status })
          } else {
            setLoad({ state: 'failed', message: 'baraza is no longer running. Restart it with `baraza serve`.' })
          }
        },
      )
    return () => {
      live = false
    }
    // `nonce` and `deps` are the point of this effect and `path` is its subject. There is
    // no exhaustive-deps rule running here to disable: `lint` is `tsc --noEmit`, with no
    // eslint config, so a suppression comment here would suppress nothing.
  }, [path, nonce, ...deps])

  return [load, useCallback(() => setNonce((n) => n + 1), [])]
}

export function App({ authorized }: { authorized: boolean }) {
  const [route, go] = useRoute()
  const [status, refreshStatus] = useLoad<Status>(authorized ? '/api/status' : null)
  const empty = status.state === 'ready' && !status.data.has_data

  // On a first run the Import screen IS the app, and the URL has to say so.
  //
  // Rendering it at whatever address the organizer landed on unmounts it the instant the
  // first import flips `has_data`, taking the import report with it: the counts, the
  // skipped files, and the events still needing a name before their guests can be used.
  //
  // Replaced rather than pushed: this is the app correcting an address, not a navigation.
  useEffect(() => {
    if (empty && route.name !== 'import') go({ name: 'import' }, { replace: true })
  }, [empty, route.name, go])

  if (!authorized) return <NoToken />
  if (status.state === 'loading') return <Chrome route={route}><p className="muted">Reading your store…</p></Chrome>
  if (status.state === 'failed') {
    return <Chrome route={route}><Problem kind="store" message={status.message} status={status.status} /></Chrome>
  }

  // Before anything is imported, import IS the screen: an empty overview would be four
  // zeroes and three empty charts, which teaches nothing. The nav is hidden because
  // there is nowhere useful for it to go yet.
  //
  // Rendered through the SAME tree as every other screen, deliberately. Returning a
  // different shape here — `Chrome > ImportScreen` rather than `Chrome > Screen >
  // ImportScreen` — meant React saw a new element at that position the moment `has_data`
  // flipped and remounted the screen, losing its report even once the route above was
  // correct. Both halves are needed: the address, and one tree.
  const showing: Route = empty ? { name: 'import' } : route

  return (
    <Chrome route={showing} bare={empty}>
      <Screen route={showing} status={status.data} onImported={refreshStatus} />
    </Chrome>
  )
}

function Screen({
  route,
  status,
  onImported,
}: {
  route: Route
  status: Status
  onImported: () => void
}) {
  switch (route.name) {
    case 'people':
      return <PeopleScreen />
    case 'person':
      return <OnePerson memberId={route.memberId} />
    case 'returning':
      return <ReturningScreen />
    // Must stay reachable WITH data present. This is the only screen that can read
    // files, so gating it on an empty store makes the core loop — drop next month's
    // export in, press refresh — unreachable the moment the first import works.
    case 'import':
      return <ImportScreen status={status} onImported={onImported} />
    case 'settings':
      return <SettingsScreen />
    default:
      return <OverviewScreen />
  }
}

function OverviewScreen() {
  const [load] = useLoad<OverviewData>('/api/overview')
  return unwrap(load, (data) => <Overview data={data} />)
}

function PeopleScreen() {
  const [load] = useLoad<{ people: Person[] }>('/api/people')
  return unwrap(load, (data) => <People people={data.people} />)
}

function ReturningScreen() {
  const [load] = useLoad<Retention>('/api/retention')
  return unwrap(load, (data) => <Returning retention={data} />)
}

function OnePerson({ memberId }: { memberId: string }) {
  const [load] = useLoad<{ member: Person; timeline: TimelineEntry[] }>(
    `/api/people/${encodeURIComponent(memberId)}`,
    [memberId],
  )
  // `kind="person"`: a member id that is not in the store says nothing about the store.
  return unwrap(load, (data) => <PersonScreen person={data.member} timeline={data.timeline} />, 'person')
}

function unwrap<T>(load: Load<T>, render: (data: T) => React.ReactNode, kind: ProblemKind = 'screen') {
  if (load.state === 'loading') return <p className="muted">Working it out…</p>
  if (load.state === 'failed') return <Problem kind={kind} message={load.message} status={load.status} />
  return <>{render(load.data)}</>
}

function Chrome({ route, children, bare }: { route: Route; children: React.ReactNode; bare?: boolean }) {
  return (
    <div className="shell">
      <header className="shell__bar">
        <img className="shell__mark" src={wordmark} alt="baraza" width={240} height={64} />
        {!bare && (
          <nav className="nav">
            <NavLink to={{ name: 'overview' }} current={route.name === 'overview'}>Overview</NavLink>
            <NavLink to={{ name: 'people' }} current={route.name === 'people' || route.name === 'person'}>People</NavLink>
            <NavLink to={{ name: 'returning' }} current={route.name === 'returning'}>Returning</NavLink>
            <NavLink to={{ name: 'import' }} current={route.name === 'import'}>Import</NavLink>
            <NavLink to={{ name: 'settings' }} current={route.name === 'settings'}>Settings</NavLink>
          </nav>
        )}
      </header>
      <main className="shell__main">{children}</main>
    </div>
  )
}

function NavLink({ to, current, children }: { to: Route; current: boolean; children: React.ReactNode }) {
  return (
    <Link to={to} className={current ? 'nav__link nav__link--current' : 'nav__link'}>
      {children}
    </Link>
  )
}

type ProblemKind = 'store' | 'person' | 'screen'

/**
 * What went wrong, headlined by **what actually went wrong**.
 *
 * This said "baraza could not read your store" for every failure the app could have —
 * a member id that is not there, a session that has expired, a screen whose endpoint
 * refused. Two of those are not about the store at all, and the one
 * that is has completely different advice from the other two. An organizer told their
 * data file is unreadable will go looking at their data file.
 *
 * The session case matters most: it is the single likeliest way anyone gets stuck — a
 * bookmarked address, a second tab, a restarted server — and the fix is one sentence
 * they can act on immediately.
 */
function Problem({ kind, message, status }: { kind: ProblemKind; message: string; status?: number }) {
  // The status code, not the words in the message. This was a regex over `message`, and
  // `ApiError` carried the code all along — so a store at `C:\Users\me\Sessions 2026\`
  // failing to open was headlined "This session has ended" and given recovery advice that
  // could not work.
  const expired = status === 401 || status === 403
  const heading = expired
    ? 'This session has ended'
    : kind === 'person'
      ? 'No such person in this store'
      : kind === 'store'
        ? 'baraza could not read your store'
        : 'That screen could not be loaded'

  return (
    <div className="notice notice--problem" role="alert">
      <h2>{heading}</h2>
      <p>{message}</p>
      {expired && (
        <p className="muted">
          Reopen the link from your terminal. Lost the window? Stop the server and run{' '}
          <code className="mono">baraza serve</code> again.
        </p>
      )}
      {kind === 'person' && !expired && (
        <p className="muted">
          <Link to={{ name: 'people' }}>Back to everyone</Link>
        </p>
      )}
    </div>
  )
}

/**
 * Opening the app without the token is the single most likely way an organizer gets
 * stuck — a bookmarked address, a second tab, a restarted server — so this says what
 * to do rather than showing an error.
 */
function NoToken() {
  return (
    <div className="notice">
      <h1>Open the link from your terminal</h1>
      <p>
        baraza gives that window a one-time link containing this session&apos;s key. Bookmarking the
        address without it lands you here.
      </p>
      <p className="muted">
        Lost the window? Stop the server and run <code className="mono">baraza serve</code> again.
      </p>
    </div>
  )
}
