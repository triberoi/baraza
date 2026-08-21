/**
 * Screen 4 — one person. Every event since they first appeared, **including the ones
 * they ignored.**
 *
 * That inclusion is the whole screen. A list of what someone attended cannot show a
 * member going quiet; a list of everything that was on offer since they arrived can,
 * and it is what turns a no-show from a row into a pattern. So an event with no record
 * is drawn — faintly, as a rule rather than a chip — instead of being skipped.
 */
import { EvidenceMark } from '../components/evidence'
import { Link } from '../router'
import { Stage } from './People'
import { percent, shortDate } from '../viz'
import type { Person, TimelineEntry } from '../api'

// "Came" on a registration-only event means "said yes, and nobody was at the door to
// know either way". The row's mark is what carries that, rather than a second set of
// labels — two vocabularies for one status is how a reader ends up trusting neither.
const STATUS_LABEL: Record<string, string> = {
  attended: 'Came',
  no_show: 'Said yes, did not come',
  registered: 'Registered',
  cancelled: 'Cancelled',
  waitlisted: 'Waitlisted',
  invited: 'Invited',
}

export function PersonScreen({ person, timeline }: { person: Person; timeline: TimelineEntry[] }) {
  return (
    <>
      <p className="crumb">
        <Link to={{ name: 'people' }}>← People</Link>
      </p>
      <header className="person">
        <h1>{person.name ?? <span className="muted">unnamed</span>}</h1>
        <Stage lifecycle={person.lifecycle} />
      </header>
      {person.email && <p className="muted mono">{person.email}</p>}

      <dl className="tiles tiles--tight">
        <Fact label="Score" value={person.score === null ? '—' : String(person.score)} />
        <Fact
          label="Attended"
          value={String(person.events_attended)}
          // Said on the person's own page, not only in the roster. Someone opens this
          // screen to check a total they doubt, and a total that cannot say how it was
          // arrived at gives them nothing to check.
          note={
            person.unmeasured_attendances > 0
              ? `${person.unmeasured_attendances} at an event where nobody was checked in`
              : undefined
          }
        />
        <Fact label="No-shows" value={String(person.no_shows)} />
        <Fact
          label="Of what was on"
          value={person.opportunities ? percent(person.attendance_rate) : '—'}
          note={person.opportunities ? `${person.opportunities} since they first appeared` : 'nothing yet'}
        />
      </dl>

      <h2 className="section">Since they first appeared</h2>
      <ol className="timeline">
        {timeline.map((entry) => (
          <li key={entry.event_id} className={`timeline__row timeline__row--${entry.status ?? 'absent'}`}>
            <span className="timeline__when muted tabular">{shortDate(entry.starts_at)}</span>
            <span className="timeline__what">
              {entry.title}
              <EvidenceMark evidence={entry.attendance_evidence} />
            </span>
            <span className="timeline__status">
              {entry.status ? (
                STATUS_LABEL[entry.status] ?? entry.status
              ) : new Date(entry.starts_at) > new Date() ? (
                /* A FUTURE event with no record is not a person going quiet — it has
                   not happened yet and nobody could have registered late for it. The
                   timeline deliberately includes future events (the server sends them;
                   "they have not signed up for next month" is worth seeing), and they
                   landed in the branch below labelled "Did not register", which reads
                   as a judgment about someone who has done nothing wrong. */
                <span className="muted">Not yet</span>
              ) : (
                /* Not "nothing" — the event happened and they did not engage with it,
                   which is different from cancelling and is the thing to notice. */
                <span className="muted">Did not register</span>
              )}
            </span>
          </li>
        ))}
      </ol>
    </>
  )
}

function Fact({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="tile">
      <dt className="label">{label}</dt>
      <dd className="tile__value">{value}</dd>
      {note && <p className="muted tile__note">{note}</p>}
    </div>
  )
}
