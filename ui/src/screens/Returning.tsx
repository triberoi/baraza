/**
 * Screen 5 — returning. Two views of the question a recurring series lives or dies on:
 * are the people who show up once becoming people who show up again, and is that getting
 * better or worse?
 *
 * Per event first, per month second. A per-event row is the whole room at one event and
 * can be read from the second event onward; a grid row is the first-timers of one month
 * and needs a calendar long enough to have months in it. On a young calendar the grid is
 * mostly blanks, which is why the readable one goes on top.
 *
 * The prose above each is not decoration. A retention grid is the one chart here
 * that nobody reads correctly on first sight, and the two blank kinds — no event that
 * month, and a month that has not arrived — are exactly what a first-time reader
 * misreads as "they all left".
 */
import { EventCohorts, RetentionGrid } from '../components/charts'
import type { Retention } from '../api'

export function Returning({ retention }: { retention: Retention }) {
  return (
    <>
      <h1>Returning</h1>
      {/* Per event first, per month below. The two answer different questions and the
          per-event one is readable from the very first pair of events, where the grid is
          still almost all blanks. */}
      <p className="lede">
        Of the people in the room at one event, how many came to a later one. Everyone who
        attended counts, regulars as well as first-timers.
      </p>
      <p className="muted note">
        The most recent event has no figure. Nothing has happened since for its room to come
        back to.
      </p>
      <EventCohorts cohorts={retention.event_cohorts} />

      <h2>By month</h2>
      <p className="lede">
        Everyone who first attended in the same month is one row. Reading across shows how many of
        them came back in the months after.
      </p>
      <p className="muted note">
        A blank cell means the question could not be asked: either no event ran that month, or
        the month has not arrived yet. It never means nobody came.
      </p>
      <RetentionGrid retention={retention} />
    </>
  )
}
