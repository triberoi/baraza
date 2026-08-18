/**
 * Screen 5 — returning. The cohort retention grid, and the question a recurring series
 * actually lives or dies on: are the people who show up once becoming people who show
 * up again, and is that getting better or worse?
 *
 * The prose above the grid is not decoration. A retention grid is the one chart here
 * that nobody reads correctly on first sight, and the two blank kinds — no event that
 * month, and a month that has not arrived — are exactly what a first-time reader
 * misreads as "they all left".
 */
import { RetentionGrid } from '../components/charts'
import type { Retention } from '../api'

export function Returning({ retention }: { retention: Retention }) {
  return (
    <>
      <h1>Returning</h1>
      <p className="lede">
        Everyone who first attended in the same month is one row. Reading across shows how many of
        them came back in the months after.
      </p>
      <p className="muted note">
        A blank cell means the question could not be asked — either no event ran that month, or the
        month has not arrived yet. It never means nobody came.
      </p>
      <RetentionGrid retention={retention} />
    </>
  )
}
