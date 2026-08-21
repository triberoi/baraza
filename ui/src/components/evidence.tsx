/**
 * The mark for an event that recorded no attendance.
 *
 * One component because the fact appears on five surfaces — the turnout bars, the
 * turnout table, the cohort table, a person's timeline and the roster — and five
 * hand-written versions of the same phrase read as five different conditions. It is
 * deliberately not a tooltip: the counts beside it are registrations rather than people
 * seen, and a reader who has to hover to learn that will not hover.
 */
import { REGISTRATION_ONLY, REGISTRATION_ONLY_LABEL } from '../viz'

const HINT =
  'Nobody was checked in at this event, so everyone who said yes is counted as having ' +
  'attended. Nobody counted them at the door, so the event has no turnout figure.'

/** Renders nothing for a measured event, so a caller can pass any event's value. */
export function EvidenceMark({ evidence }: { evidence: string }) {
  if (evidence !== REGISTRATION_ONLY) return null
  return (
    <span className="evidence-mark" title={HINT}>
      {REGISTRATION_ONLY_LABEL}
    </span>
  )
}
