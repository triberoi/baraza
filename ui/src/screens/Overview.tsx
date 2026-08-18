/**
 * Screen 2 — overview. Attendance split new vs returning, and the lifecycle mix.
 *
 * The stat tiles above the charts are stat tiles rather than charts on purpose: a
 * single number is not a one-bar bar chart. Each is a figure the organizer would say
 * out loud about their community.
 */
import { LifecycleMix, Turnout } from '../components/charts'
import { percent } from '../viz'
import type { Overview as OverviewData } from '../api'

export function Overview({ data }: { data: OverviewData }) {
  const past = data.events
  const attended = past.reduce((sum, e) => sum + e.attended, 0)
  const committed = past.reduce((sum, e) => sum + e.committed, 0)
  const newcomers = past.reduce((sum, e) => sum + e.new_attendees, 0)

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
        />
        <Tile label="First-time attendances" value={String(newcomers)} note="people who came for the first time" />
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
function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  // The note lives INSIDE the <dd>: a <div> group in a <dl> may hold only dt/dd, so a
  // sibling <p> made the list invalid — the axe pass's second day-one finding. The
  // note qualifies the value ("of those who said yes"), so the definition is where it
  // belongs semantically too.
  return (
    <div className="tile">
      <dt className="label">{label}</dt>
      <dd className="tile__value">
        {value}
        {note && <span className="muted tile__note">{note}</span>}
      </dd>
    </div>
  )
}
