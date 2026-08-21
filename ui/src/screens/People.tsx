/**
 * Screen 3 — the roster. The plan calls it "the table view for every chart", and it is
 * literally that: every number the other screens draw is a column here.
 *
 * **Sorting is client-side and stable.** The API answers in member-id order, which is
 * a hash and means nothing to a person; the default here is score, descending, because
 * the question this screen answers is "who are my people". Ties break on name so two
 * runs never disagree.
 *
 * A missing score renders as an em dash, never as 0. The API went out of its way to
 * distinguish "no history yet" from "had chances and took none", and a table that
 * prints 0 for both throws that away at the last step.
 *
 * The Attended column carries the same obligation one step further: where some of a
 * person's attendances came from an event that measured nobody, the column says so on
 * the row rather than leaving the total to be read as people seen.
 */
import { useMemo, useState } from 'react'

import { Link } from '../router'
import { FALLBACK_FILL, LIFECYCLE_FILL, LIFECYCLE_LABEL, inkOn, percent } from '../viz'
import type { Person } from '../api'

const UNMEASURED_HINT =
  'This many of their attendances came from an event where nobody was checked in. They ' +
  'said yes, and saying yes is the only evidence there is.'

type Column = 'name' | 'score' | 'lifecycle' | 'events_attended' | 'no_shows' | 'attendance_rate'

export function People({ people }: { people: Person[] }) {
  const [sort, setSort] = useState<{ by: Column; desc: boolean }>({ by: 'score', desc: true })
  const [needle, setNeedle] = useState('')

  const rows = useMemo(() => {
    const query = needle.trim().toLowerCase()
    const matched = query
      ? people.filter(
          (p) => (p.name ?? '').toLowerCase().includes(query) || (p.email ?? '').toLowerCase().includes(query),
        )
      : people
    const key = (p: Person): string | number => {
      if (sort.by === 'name') return (p.name ?? p.member_id).toLowerCase()
      if (sort.by === 'lifecycle') return p.lifecycle
      if (sort.by === 'score') return p.score ?? -1
      return p[sort.by]
    }
    // **A missing score is not a low score, and it does not take part in the ordering.**
    //
    // A sentinel does not work: `?? -1` sorts below 0, so ascending puts everyone whose
    // only event has not happened yet ABOVE the people who genuinely turned up to
    // nothing, and those two groups need opposite actions from the organizer.
    //
    // Sorting them out of the comparison entirely is the only answer that holds in both
    // directions: no data goes last whichever way the column points, which is what the
    // old comment said and what a reader scanning a sorted table expects.
    const unscored = (p: Person) => sort.by === 'score' && p.score === null
    return [...matched].sort((a, b) => {
      if (unscored(a) !== unscored(b)) return unscored(a) ? 1 : -1
      const [x, y] = [key(a), key(b)]
      if (x === y) return (a.name ?? a.member_id).localeCompare(b.name ?? b.member_id)
      return (x < y ? -1 : 1) * (sort.desc ? -1 : 1)
    })
  }, [people, sort, needle])

  // `aria-sort` belongs on the columnheader, not the button inside it, where it is an
  // unsupported attribute.
  const header = (by: Column, text: string) => (
    <th scope="col" aria-sort={sort.by === by ? (sort.desc ? 'descending' : 'ascending') : 'none'}>
      <button
        className="sorter"
        onClick={() => setSort((s) => ({ by, desc: s.by === by ? !s.desc : true }))}
      >
        {text}
        <span className="sorter__mark" aria-hidden="true">{sort.by === by ? (sort.desc ? '↓' : '↑') : ''}</span>
      </button>
    </th>
  )

  return (
    <>
      <h1>People</h1>
      <div className="toolbar">
        <label className="field field--inline">
          <span className="label">Find</span>
          <input value={needle} onChange={(e) => setNeedle(e.target.value)} placeholder="name or email" />
        </label>
        <span className="muted">
          {rows.length} of {people.length}
        </span>
      </div>

      <div className="twin__scroll">
        <table className="roster">
          <thead>
            <tr>
              {header('name', 'Name')}
              {header('lifecycle', 'Stage')}
              {header('score', 'Score')}
              {header('events_attended', 'Attended')}
              {header('no_shows', 'No-shows')}
              {header('attendance_rate', 'Of what was on')}
            </tr>
          </thead>
          <tbody>
            {rows.map((person) => (
              <tr key={person.member_id}>
                <th scope="row">
                  <Link to={{ name: 'person', memberId: person.member_id }}>
                    {person.name ?? <span className="muted">unnamed</span>}
                  </Link>
                  {person.email && <span className="muted roster__email">{person.email}</span>}
                </th>
                <td><Stage lifecycle={person.lifecycle} /></td>
                <td className="tabular">
                  {person.score === null ? <span className="muted" title="nothing has happened for them yet">—</span> : person.score}
                </td>
                <td className="tabular">
                  {person.events_attended}
                  {/* Split, not annotated elsewhere. This is the row somebody checks when
                      they distrust the headline, so a member counted in from a
                      registration must not read the same as one who came through a
                      door. */}
                  {person.unmeasured_attendances > 0 && (
                    <span className="muted roster__qualifier" title={UNMEASURED_HINT}>
                      ({person.unmeasured_attendances} not measured)
                    </span>
                  )}
                </td>
                <td className="tabular">{person.no_shows}</td>
                <td className="tabular">{person.opportunities ? percent(person.attendance_rate) : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length === 0 && <p className="muted">Nobody matches that.</p>}
    </>
  )
}

/** The stage as a chip. Same fills as the mix chart, so the two are one vocabulary. */
export function Stage({ lifecycle }: { lifecycle: string }) {
  const fill = LIFECYCLE_FILL[lifecycle] ?? FALLBACK_FILL
  return (
    <span className="chip" style={{ background: fill, color: inkOn(fill) }}>
      {LIFECYCLE_LABEL[lifecycle] ?? lifecycle}
    </span>
  )
}
