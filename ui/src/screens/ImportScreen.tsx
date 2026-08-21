/**
 * The import screen — the only place in the app that can read files.
 *
 * It is the first thing an organizer sees before the tool knows anything, AND a screen on
 * the nav forever after. Both, or the core loop — drop next month's export in the folder,
 * press refresh — becomes unreachable the moment the first import works, along with naming
 * the events Luma could not identify.
 *
 * Two paths, side by side, because the free one is the spine: anyone can export a
 * guest list, and only a paying Luma account can hand over an API key. The folder is
 * therefore first and needs no explanation of what a key is.
 *
 * **Pending events are the interesting state.** An event the resolver could not name
 * is not an error and must not read as one — the organizer supplies a name and a date,
 * then reads the folder again to attach its guests. That form is right here rather
 * than behind a link, because a list of unnamed ids with no way to act on them is a
 * dead end. The list itself comes from the store, so it is still here after a reload
 * or when the import ran in the terminal.
 */
import { useEffect, useRef, useState } from 'react'

import { ApiError, get, post, put, type ImportReport, type PendingEvent, type PendingList, type Status } from '../api'
import { Link } from '../router'

export function ImportScreen({ status, onImported }: { status: Status; onImported: () => void }) {
  const [folder, setFolder] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState<'folder' | 'luma' | null>(null)
  const [report, setReport] = useState<ImportReport | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  // Pending events come from the STORE (`/api/import/pending`), not from the last import's
  // response. Rendered from the response alone they exist exactly once, so a reload, a
  // navigation, or arriving from the CLI finds this screen with no sign work is waiting.
  const [pendingList, setPendingList] = useState<PendingList | null>(null)
  // Whether the organizer has typed in the folder box. The last-read folder is offered
  // back as a convenience, and a convenience must never overwrite what they typed.
  const folderTouched = useRef(false)

  function refreshPending() {
    get<PendingList>('/api/import/pending')
      .then((list) => {
        setPendingList(list)
        if (!folderTouched.current && list.folder) setFolder(list.folder)
      })
      .catch(() => setPendingList(null)) // absence of the convenience, never an error state
  }

  useEffect(refreshPending, [])

  async function run(which: 'folder' | 'luma') {
    setBusy(which)
    setProblem(null)
    // The previous run's report goes too. Left up, it sat directly under
    // "That did not work" still telling the organizer to read a folder that is no longer
    // in the box — with its Save form live against events this run never saw.
    setReport(null)
    try {
      const body = which === 'folder' ? { path: folder } : apiKey ? { api_key: apiKey } : {}
      const result = await post<ImportReport>(`/api/import/${which}`, body)
      setReport(result)
      setApiKey('')
      if (result.events > 0) onImported()
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : String(error))
    } finally {
      setBusy(null)
      refreshPending()
    }
  }

  return (
    <>
      <h1>Bring in your Luma events</h1>
      <p className="lede">
        Everything stays on this machine. baraza reads your exports into a single file beside them.
        No account, and nothing leaves your laptop.
      </p>

      <div className="panels">
        <section className="panel">
          <h2>From a folder of exports</h2>
          <p className="muted">
            Any Luma account can export a guest list from an event page. Point baraza at the folder
            you downloaded them to.
          </p>
          {/* A real <form>: typing a path and pressing Enter did nothing, because these
              inputs sat in a <label> with no form to submit. */}
          <form
            onSubmit={(e) => {
              e.preventDefault()
              if (folder && busy === null) void run('folder')
            }}
          >
            <label className="field">
              <span className="label">Folder</span>
              <input
                value={folder}
                onChange={(e) => {
                  folderTouched.current = true
                  setFolder(e.target.value)
                }}
                placeholder="C:\Users\you\Downloads"
                spellCheck={false}
              />
            </label>
            <button className="button" type="submit" disabled={!folder || busy !== null}>
              {busy === 'folder' ? 'Reading…' : 'Read this folder'}
            </button>
          </form>
        </section>

        <section className="panel">
          <h2>From the Luma API</h2>
          <p className="muted">
            A paid Luma plan gives you an API key. baraza reads the whole calendar directly, including
            event names and venues.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault()
              if (busy === null && (apiKey || status.luma_key_configured)) void run('luma')
            }}
          >
            <label className="field">
              <span className="label">API key</span>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={status.luma_key_configured ? 'Leave blank to reuse the saved key' : 'secret-…'}
                spellCheck={false}
              />
            </label>
            <button
              className="button"
              type="submit"
              disabled={busy !== null || (!apiKey && !status.luma_key_configured)}
            >
              {busy === 'luma' ? 'Reading…' : status.luma_key_configured ? 'Refresh from Luma' : 'Read my calendar'}
            </button>
          </form>
          {status.luma_key_configured && (
            <p className="muted note">Your key is saved on this machine, beside your data.</p>
          )}
        </section>
      </div>

      {problem && (
        <div className="notice notice--problem" role="alert">
          <h3>That did not work</h3>
          <p>{problem}</p>
        </div>
      )}

      {report && <Report report={report} />}

      {/* `onNamed` deliberately does NOT refetch the pending list: the store filters a
          named event out on the next fetch, and refetching here would unmount the row
          mid-flow — taking its "Saved — read the folder again" instruction with it. */}
      {pendingList && pendingList.pending.length > 0 && (
        <Pending pending={pendingList.pending} onNamed={onImported} />
      )}
    </>
  )
}

function Report({ report }: { report: ImportReport }) {
  return (
    <section className="report">
      <h2>
        {report.events} {report.events === 1 ? 'event' : 'events'}, {report.members}{' '}
        {report.members === 1 ? 'person' : 'people'}
      </h2>
      {/* The one line that stops a first import being a dead end: the nav quietly
          appearing top-right is not something a first-time organizer knows to look for. */}
      {report.events > 0 && (
        <p>
          <Link to={{ name: 'overview' }}>See your overview →</Link>
        </p>
      )}
      {report.files_skipped && report.files_skipped.length > 0 && (
        <p className="muted">
          Not Luma exports, so skipped: {report.files_skipped.join(', ')}.
        </p>
      )}
      {report.warnings.length > 0 && (
        <details className="twin">
          <summary>
            {report.warnings.length} {report.warnings.length === 1 ? 'thing' : 'things'} worth knowing
          </summary>
          <ul className="warnings">
            {/* Keyed by position, not by text: two events can produce the same warning
                sentence, and React then sees duplicate keys and drops one. The list is
                display-only and never reordered, so the index is the identity here. */}
            {report.warnings.map((warning, index) => (
              <li key={`${index}-${warning}`} className="muted">{warning}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

/**
 * The manual-entry path. Naming an event writes it to the store; its guests attach when
 * the folder is read again, which is why the copy tells the organizer to KEEP the folder.
 * Never tell them the guests are already read: it is true of the pass that read them and
 * false of the store, and an organizer who deletes their exports loses the guests.
 */
function Pending({ pending, onNamed }: { pending: PendingEvent[]; onNamed: () => void }) {
  return (
    <div className="pending">
      <h3>
        {pending.length} {pending.length === 1 ? 'event needs' : 'events need'} a name and a date
      </h3>
      <p className="muted">
        Luma could not tell baraza about {pending.length === 1 ? 'this one' : 'these'}, usually
        because the event is private. Fill these in, keep the exports where they are, and read the
        folder again. That second read attaches the guests.
      </p>
      {pending.map((event) => (
        <NameEvent key={event.luma_event_id} event={event} onNamed={onNamed} />
      ))}
    </div>
  )
}

function NameEvent({ event, onNamed }: { event: PendingEvent; onNamed: () => void }) {
  const [title, setTitle] = useState(event.inferred_title ?? '')
  const [date, setDate] = useState('')
  const [saved, setSaved] = useState(false)
  const [saving, setSaving] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)

  async function save() {
    // In-flight guard, mirroring `busy` on the folder form above. Without
    // it three fast clicks sent three PUTs. They are idempotent with an identical body,
    // so nothing broke — but the folder form had already closed this and this one had not.
    if (saving) return
    setSaving(true)
    try {
      // The API refuses a naive datetime — a date with no zone reads back as a
      // different instant — so the browser's own offset is attached here.
      await put(`/api/events/${encodeURIComponent(event.luma_event_id)}`, {
        title,
        starts_at: new Date(`${date}T12:00:00`).toISOString(),
      })
      setSaved(true)
      // `onNamed` refreshes `/api/status`. It no longer unmounts this screen — `useLoad`
      // keeps showing what it has while refetching — and it is still
      // wanted, because naming the last pending event is what can flip `has_data`.
      onNamed()
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : String(error))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="pending__row">
      <div className="pending__id">
        <code className="mono">{event.luma_event_id}</code>
        <span className="muted">
          {event.guests} {event.guests === 1 ? 'guest' : 'guests'} · {event.sources.join(', ')}
        </span>
      </div>
      {saved ? (
        <p className="saved">Saved. Read the folder again to bring its guests in.</p>
      ) : (
        <div className="pending__form">
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Event name" />
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          <button className="button button--quiet" disabled={!title || !date || saving} onClick={save}>
            Save
          </button>
        </div>
      )}
      {problem && <p className="problem">{problem}</p>}
    </div>
  )
}
