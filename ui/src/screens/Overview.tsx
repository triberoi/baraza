/**
 * Screen 2 — overview. Attendance split new vs returning, and the lifecycle mix.
 *
 * The stat tiles above the charts are stat tiles rather than charts on purpose: a
 * single number is not a one-bar bar chart. Each is a figure the organizer would say
 * out loud about their community.
 */
import { LifecycleMix, Turnout } from '../components/charts'
import { REGISTRATION_ONLY, includesUnmeasured, measuredScope, percent } from '../viz'
import type { Overview as OverviewData } from '../api'

export function Overview({ data }: { data: OverviewData }) {
  const past = data.events
  const newcomers = past.reduce((sum, e) => sum + e.new_attendees, 0)

  // Turnout is summed over the events that RECORDED attendance. On a registration-only
  // event every committed guest reads as attended, so folding one in adds a 100% to both
  // halves — inflating the calendar figure and making the events that were scanned look
  // worse beside it. The events are still counted in "Events held": they happened.
  const measured = past.filter((e) => e.attendance_evidence !== REGISTRATION_ONLY)
  const attended = measured.reduce((sum, e) => sum + e.attended, 0)
  const committed = measured.reduce((sum, e) => sum + e.committed, 0)
  const unmeasured = past.length - measured.length

  return (
    <>
      <h1>Overview</h1>
      <dl className="tiles">
        <Tile label="Events held" value={String(past.length)} />
        <Tile label="People" value={String(data.people_scored)} />
        <Tile
          label="Turned up"
          value={committed ? percent(attended / committed) : '—'}
          note={committed ? 'of those who said yes' : 'nobody has said yes yet'}
          // Beside the number, not under a tooltip. A reader who cannot see that the
          // denominator skipped events will quote this as the whole calendar's turnout.
          scope={committed ? measuredScope(measured.length, past.length) : null}
        />
        <Tile
          label="First-time attendances"
          value={String(newcomers)}
          note="people who came for the first time"
          // The opposite scope to the tile above: this one COUNTS the unmeasured events,
          // where a registration stands in for an attendance.
          scope={includesUnmeasured(unmeasured, past.length)}
        />
      </dl>

      <div className="split">
        <Turnout events={past} />
        <LifecycleMix mix={data.lifecycle_mix} total={data.people_scored} />
      </div>
    </>
  )
}

/**
 * A number and its name. Proportional figures, not tabular: equal-width digits make a
 * large standalone number look loose, and nothing here lines up in a column.
 */
function Tile({
  label,
  value,
  note,
  scope,
}: {
  label: string
  value: string
  note?: string
  /** How much of the calendar this figure covers, when it is not all of it. Its own line
   *  rather than appended to `note`: the note says what the number means and this says
   *  what it was taken over, and running them together reads as one sentence. */
  scope?: string | null
}) {
  // The note lives INSIDE the <dd>: a <div> group in a <dl> may hold only dt/dd, so a
  // sibling <p> makes the list invalid. The note qualifies the value, so the definition
  // is where it belongs anyway.
  return (
    <div className="tile">
      <dt className="label">{label}</dt>
      <dd className="tile__value">
        {value}
        {note && <span className="muted tile__note">{note}</span>}
        {scope && <span className="muted tile__note tile__scope">{scope}</span>}
      </dd>
    </div>
  )
}
