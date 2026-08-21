/**
 * The three charts, and the table each one is also readable as.
 *
 * **Every chart here ships a table twin, and that is not optional.** The palette
 * validator returns a contrast warning on the lighter neutrals against a white page —
 * accepted, because the brand is deliberately restrained — and a contrast warning
 * obligates relief: visible labels or a table view. So each chart has a `<details>`
 * beside it holding the same numbers as text. It is also the plain answer to
 * "what exactly is that bar", which someone asks of every chart eventually.
 *
 * Mark conventions, applied throughout: thin marks, a 2px surface gap between adjacent
 * fills rather than a border drawn around them, hairline axes one shade off the
 * surface, and labels only where they fit — never a number on every segment.
 */
import { FALLBACK_FILL, LIFECYCLE_FILL, LIFECYCLE_LABEL, LIFECYCLE_ORDER, TURNOUT_FILL, RETENTION_RAMP, inkOn, percent, retentionFill, shortDate, shortMonth } from '../viz'
import { EvidenceMark } from './evidence'
import type { Cohort, EventCohort, EventTurnout, Retention } from '../api'

function TableTwin({ caption, children }: { caption: string; children: React.ReactNode }) {
  return (
    <details className="twin">
      <summary>{caption}</summary>
      <div className="twin__scroll">{children}</div>
    </details>
  )
}

/* --- lifecycle mix ---------------------------------------------------------- */
/**
 * One stacked bar. The middle three are an ordinal ramp because first-timer → regular
 * → champion is an order; lapsed is red because the brand reserves red for what needs
 * action, and it is the one group here an organizer can act on.
 */
export function LifecycleMix({ mix, total }: { mix: Record<string, number>; total: number }) {
  const present = LIFECYCLE_ORDER.filter((key) => (mix[key] ?? 0) > 0)
  if (total === 0) return <p className="muted">No members yet.</p>

  return (
    <figure className="chart">
      <figcaption className="label">Lifecycle mix</figcaption>
      <div className="stack" role="img" aria-label={present.map((k) => `${LIFECYCLE_LABEL[k]}: ${mix[k]}`).join(', ')}>
        {present.map((key) => {
          const count = mix[key] ?? 0
          const share = count / total
          // The `??` here and on the other index lookups are NOT unreachable padding:
          // `noUncheckedIndexedAccess` is on, so indexing a Record yields
          // `string | undefined` and the compiler refuses the call without them.
          // Checked by deleting one — `tsc` fails.
          const fill = LIFECYCLE_FILL[key] ?? FALLBACK_FILL
          return (
            <div
              key={key}
              className="stack__seg"
              // `flexBasis: 0` is what makes `flexGrow` a share. Without it every
              // segment starts at its CONTENT width and grows from there, so the ones
              // carrying a visible count rendered wider than their proportion and the
              // unlabelled ones narrower — a bar that does not add up to what it claims
              // to show. `minWidth: 0` stops a long number putting
              // the floor back under it.
              style={{ flexGrow: count, flexBasis: 0, minWidth: 0, background: fill, color: inkOn(fill) }}
              title={`${LIFECYCLE_LABEL[key]}: ${count}`}
            >
              {/* Only where it fits. A clipped label is worse than no label — the
                  count is in the legend and the table either way. */}
              {share > 0.12 && <span className="stack__value tabular">{count}</span>}
            </div>
          )
        })}
      </div>
      <ul className="legend">
        {LIFECYCLE_ORDER.map((key) => (
          <li key={key}>
            <span className="legend__swatch" style={{ background: LIFECYCLE_FILL[key] }} aria-hidden="true" />
            {LIFECYCLE_LABEL[key]} <span className="muted tabular">{mix[key] ?? 0}</span>
          </li>
        ))}
      </ul>
      <TableTwin caption="Lifecycle mix as a table">
        <table>
          <thead>
            <tr><th scope="col">Stage</th><th scope="col">Members</th><th scope="col">Share</th></tr>
          </thead>
          <tbody>
            {LIFECYCLE_ORDER.map((key) => (
              <tr key={key}>
                <th scope="row">{LIFECYCLE_LABEL[key]}</th>
                <td>{mix[key] ?? 0}</td>
                <td>{percent((mix[key] ?? 0) / total)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableTwin>
    </figure>
  )
}

/* --- turnout ---------------------------------------------------------------- */
/**
 * Attendance per event, split new vs returning. Stacked because the two parts compose
 * the whole — they sum to everyone who turned up — and a grouped pair would invite
 * reading them as competing series.
 *
 * No-shows are **not** a third segment: they are not attendance, and stacking them
 * would make the bar's height mean two different things. They are in the table.
 *
 * The application funnel appears only when some event in view actually had one. Three
 * columns of em dashes on every open event would cost the table its width to say nothing.
 */
const UNDECIDED_HINT =
  'Still in the queue when the event ended. Luma does not record whether the organizer ' +
  'declined to act, ran out of review time, hit capacity, or never saw the request.'

const DECLINED_HINT =
  'One Luma word for two things: the organizer turning an application down, and the guest ' +
  'saying they are not coming. They cannot be told apart.'

export function Turnout({ events }: { events: EventTurnout[] }) {
  if (events.length === 0) return <p className="muted">No events have happened yet.</p>
  const tallest = Math.max(...events.map((e) => e.attended), 1)
  const funnel = events.some((event) => event.applied !== null)

  return (
    <figure className="chart">
      <figcaption className="label">Turnout: new vs returning</figcaption>
      <ul className="bars">
        {events.map((event) => (
          <li key={event.event_id} className="bars__row">
            <span className="bars__name" title={event.title}>{event.title}</span>
            <span className="bars__track">
              <span
                className="bars__seg"
                style={{ width: `${(event.returning_attendees / tallest) * 100}%`, background: TURNOUT_FILL.returning }}
                title={`Returning: ${event.returning_attendees}`}
              />
              <span
                className="bars__seg"
                style={{ width: `${(event.new_attendees / tallest) * 100}%`, background: TURNOUT_FILL.new }}
                title={`New: ${event.new_attendees}`}
              />
            </span>
            <span className="bars__total tabular">{event.attended}</span>
            {/* Its own cell, not inside `bars__name` — that one truncates with an
                ellipsis, which would eat the mark on exactly the long titles that need
                it most. */}
            <EvidenceMark evidence={event.attendance_evidence} />
          </li>
        ))}
      </ul>
      <ul className="legend">
        <li><span className="legend__swatch" style={{ background: TURNOUT_FILL.returning }} aria-hidden="true" />Returning</li>
        <li><span className="legend__swatch" style={{ background: TURNOUT_FILL.new }} aria-hidden="true" />New</li>
      </ul>
      <TableTwin caption="Turnout as a table">
        <table>
          <thead>
            <tr>
              <th scope="col">Event</th><th scope="col">Date</th><th scope="col">New</th>
              <th scope="col">Returning</th><th scope="col">Attended</th>
              <th scope="col">No-shows</th><th scope="col">Turned up</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.event_id}>
                <th scope="row">
                  {event.title}
                  <EvidenceMark evidence={event.attendance_evidence} />
                </th>
                <td>{shortDate(event.starts_at)}</td>
                <td>{event.new_attendees}</td>
                <td>{event.returning_attendees}</td>
                <td>{event.attended}</td>
                <td>{event.no_shows}</td>
                {/* Of those who said yes — a cancellation is not a broken promise. */}
                {/* Guarded on the rate itself, not on `committed`: the API now sends
                    null when there was nobody to count, and a zero turnout against a
                    real roster is a 0% we must still print. */}
                <td>{event.show_rate === null ? '—' : percent(event.show_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </TableTwin>
      {/* The limits, in the open. A tooltip states them where the number is read; this
          says them where the number cannot be missed, because the counts look more
          precise than the export they come from. */}
      {funnel && (
        <TableTwin caption="Applications as a table">
          <table>
            <thead>
              <tr>
                <th scope="col">Event</th>
                <th scope="col">Applied</th>
                <th scope="col" title={UNDECIDED_HINT}>Awaiting a decision</th>
                <th scope="col" title={DECLINED_HINT}>Declined or withdrew</th>
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.event_id}>
                  <th scope="row">{event.title}</th>
                  {/* An event nobody could apply to has no funnel, which is not a funnel
                      of zero — the same distinction `show_rate` draws. */}
                  <td>{event.applied ?? '—'}</td>
                  <td>{event.not_admitted ?? '—'}</td>
                  <td>{event.declined_or_withdrew ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableTwin>
      )}
      {funnel && (
        <p className="figure__note">
          <strong>Awaiting a decision</strong> covers everyone still in the queue when the event
          ended. Luma does not record whether an organizer chose not to act, ran out of review
          time, hit capacity, or never saw the request.{' '}
          <strong>Declined or withdrew</strong> is one word in Luma for two different things: the
          organizer turning an application down, and the guest saying they are not coming. Neither
          count feeds the turnout figures, because nobody who was never given a place can be a
          no-show.
        </p>
      )}
    </figure>
  )
}

/* --- per-event cohort return ------------------------------------------------ */
/**
 * Of the people in the room at one event, how many came to a later one.
 *
 * A table rather than a chart, and that is the design rather than a shortcut. The rooms
 * here are tens of people, so one person is several percentage points — a bar invites
 * reading a gap that a single attendee closes. The count sits beside every rate for the
 * same reason.
 *
 * The most recent event keeps its row and shows no rate. It has had nothing to return
 * to, which is not the same as nobody returning; drawing it as 0% would report a
 * collapse every time an organizer imports the event they just ran.
 */
export function EventCohorts({ cohorts }: { cohorts: EventCohort[] }) {
  if (cohorts.length === 0) return <p className="muted">No events have happened yet.</p>

  return (
    <figure className="chart">
      <figcaption className="label">Did the room come back?</figcaption>
      <div className="twin__scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Event</th>
              <th scope="col">Date</th>
              <th scope="col">In the room</th>
              <th scope="col">Came to a later event</th>
            </tr>
          </thead>
          <tbody>
            {cohorts.map((cohort) => (
              <tr key={cohort.event_id}>
                <th scope="row">
                  {cohort.title}
                  <EvidenceMark evidence={cohort.attendance_evidence} />
                </th>
                <td>{shortDate(cohort.starts_at)}</td>
                <td className="tabular">{cohort.attended}</td>
                <td className="tabular">
                  {cohort.returned === null || cohort.return_rate === null ? (
                    '—'
                  ) : (
                    <>
                      {cohort.returned} <span className="muted">({percent(cohort.return_rate)})</span>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  )
}


/* --- retention -------------------------------------------------------------- */
/**
 * The cohort grid — the view Luma structurally cannot draw.
 *
 * The whole design problem here is the **empty cell**, and there are two kinds. A month
 * with no event is not a month the cohort skipped; a month that has not arrived is not
 * a month they failed to come to. Neither gets a step on the ramp, and they are drawn
 * differently from each other so the legend can explain both.
 */
export function RetentionGrid({ retention }: { retention: Retention }) {
  if (retention.cohorts.length === 0) {
    return <p className="muted">Nobody has attended an event yet, so there are no cohorts to follow.</p>
  }
  const active = new Set(retention.active_months)
  const width = Math.max(...retention.cohorts.map((c) => c.attended.length))

  const cellFor = (cohort: Cohort, offset: number) => {
    const start = retention.months.indexOf(cohort.month)
    const month = retention.months[start + offset]
    if (offset >= cohort.attended.length || month === undefined) return { kind: 'future' as const }
    if (!active.has(month)) return { kind: 'no-event' as const }
    const count = cohort.attended[offset]
    // A blank in the month still running is "not counted yet", not "no event". Test this
    // before the null fallback below, which would otherwise claim no event ran.
    if (month === retention.in_progress_month) return { kind: 'in-progress' as const }
    if (count === null || count === undefined) return { kind: 'no-event' as const }
    return { kind: 'rate' as const, count, rate: count / cohort.size }
  }

  return (
    <figure className="chart">
      <figcaption className="label">Repeat attendance by cohort</figcaption>
      <div className="grid__scroll">
        <table className="grid">
          <thead>
            <tr>
              <th scope="col">First attended</th>
              <th scope="col">Size</th>
              {Array.from({ length: width }, (_, i) => (
                <th key={i} scope="col" className="grid__offset">{i === 0 ? 'Month 0' : `+${i}`}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {retention.cohorts.map((cohort) => (
              <tr key={cohort.month}>
                <th scope="row">{shortMonth(cohort.month)}</th>
                <td className="tabular">{cohort.size}</td>
                {Array.from({ length: width }, (_, offset) => {
                  const cell = cellFor(cohort, offset)
                  if (cell.kind === 'future') return <td key={offset} className="grid__cell grid__cell--future" />
                  if (cell.kind === 'in-progress') {
                    return (
                      <td
                        key={offset}
                        className="grid__cell grid__cell--inprogress"
                        title="This month is still running, so it is not counted yet"
                      >
                        <span aria-label="this month is still running">…</span>
                      </td>
                    )
                  }
                  if (cell.kind === 'no-event') {
                    return (
                      <td key={offset} className="grid__cell grid__cell--noevent" title="No event that month">
                        <span aria-label="no event that month">·</span>
                      </td>
                    )
                  }
                  const fill = retentionFill(cell.rate) ?? RETENTION_RAMP[0]
                  // Column 0 is the cohort's own month, where everyone attended by
                  // definition. Calling it "12 of 12 came back" states a definition as a
                  // measurement, on the cell the rest of the row is read against.
                  const title =
                    offset === 0
                      ? `${cohort.size} first attended this month`
                      : `${cell.count} of ${cohort.size} came back`
                  return (
                    <td
                      key={offset}
                      className="grid__cell"
                      style={{ background: fill, color: inkOn(fill) }}
                      title={title}
                    >
                      <span className="tabular">{percent(cell.rate)}</span>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="legend legend--scale">
        <li className="muted">Fewer came back</li>
        {RETENTION_RAMP.map((step) => (
          <li key={step}><span className="legend__swatch" style={{ background: step }} aria-hidden="true" /></li>
        ))}
        <li className="muted">More</li>
        <li className="legend__gap"><span className="legend__swatch legend__swatch--noevent" aria-hidden="true" />No event that month</li>
        {retention.in_progress_month !== null && (
          <li className="legend__gap">
            <span className="legend__swatch legend__swatch--inprogress" aria-hidden="true" />
            This month is still running
          </li>
        )}
      </ul>
    </figure>
  )
}
