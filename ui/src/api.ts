/**
 * Talking to the local API, and holding the session token while we do.
 *
 * The token arrives once, in the URL the terminal printed, because a browser's
 * first contact with a server is a navigation and a navigation cannot carry a
 * header. Everything after that is the `X-Baraza-Token` header.
 *
 * Three things happen to it here, and each is deliberate:
 *
 * 1. **It is taken out of the address bar immediately** (`history.replaceState`).
 *    Left there it sits in the window title, in browser history, in a screenshot
 *    of the tool, and in anything the organizer copies to ask a question with.
 * 2. **It is kept in sessionStorage, not localStorage.** Per tab, gone when the
 *    tab closes. There is nothing to log out of, so the tab closing is the only
 *    logout this tool has.
 * 3. **It is never put back into a URL.** Header only, from here on.
 */

const TOKEN_KEY = 'baraza.token'
const TOKEN_HEADER = 'X-Baraza-Token'

/**
 * Take the token out of the URL and remember it. Returns whether we have one at
 * all — the app has nothing to show without it, and saying so beats a screen of
 * failed requests.
 */
export function claimToken(): boolean {
  const url = new URL(window.location.href)
  const fromUrl = url.searchParams.get('token')
  if (fromUrl) {
    sessionStorage.setItem(TOKEN_KEY, fromUrl)
    url.searchParams.delete('token')
    window.history.replaceState({}, '', url.pathname + url.search + url.hash)
  }
  return sessionStorage.getItem(TOKEN_KEY) !== null
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
  }
}

/**
 * A GET against the local API.
 *
 * No `credentials`, no cookies, no CORS mode: this is same-origin and stays that
 * way. The server refuses a foreign `Origin` outright, so a fetch that needed
 * one would be a fetch that should not exist.
 */
export async function get<T>(path: string): Promise<T> {
  return request<T>('GET', path)
}

/** A write. The browser attaches `Origin` to every non-GET, which is what the
 *  server's CSRF check reads — nothing to do here but send it. */
export async function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>('POST', path, body)
}

export async function put<T>(path: string, body: unknown): Promise<T> {
  return request<T>('PUT', path, body)
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = sessionStorage.getItem(TOKEN_KEY)
  const response = await fetch(path, {
    method,
    headers: {
      ...(token ? { [TOKEN_HEADER]: token } : {}),
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (!response.ok) {
    // The server's `detail` is written for the organizer — the Luma-side text and
    // the resolved-address kind of thing are deliberately kept out of it — so it
    // is safe to show. Fall back to the status when there is no body at all.
    const detail = await response
      .json()
      .then((payload: { detail?: string }) => payload.detail)
      .catch(() => undefined)
    throw new ApiError(response.status, detail ?? `The request failed (${response.status}).`)
  }
  return (await response.json()) as T
}

export interface Status {
  store_path: string
  events: number
  /** People the store HOLDS. Not `Overview.people_scored`, which counts people
   *  analytics could score — the two answer different questions and were both called
   *  `members`. */
  people_in_store: number
  attendances: number
  /** Why the stored thresholds cannot be used, or null. Non-null means three screens
   *  are refusing to answer and Settings is the way out. */
  settings_unusable: string | null
  has_data: boolean
  luma_key_configured: boolean
}

export interface EventTurnout {
  event_id: string
  title: string
  starts_at: string
  roster: number
  committed: number
  attended: number
  no_shows: number
  new_attendees: number
  returning_attendees: number
  show_rate: number | null
  /** The application funnel. All three are `null` together for an event that shows no
   *  sign of having required approval — three zeros would claim nobody applied to an
   *  event nobody could apply to. Read from Luma's own word, never from `status`. */
  applied: number | null
  /** Left undecided at the event's end. Luma cannot say whether the organizer declined
   *  to act, ran out of time, hit capacity, or never saw the request. */
  not_admitted: number | null
  /** Luma marks an organizer's decline and a guest's "Not Going" with the SAME word, so
   *  these cannot be told apart — which is why this is not called "denied". */
  declined_or_withdrew: number | null
  /** What this event measured — compare against CHECKED_IN / REGISTRATION_ONLY in
   *  `viz.ts`, never a literal. On a registration-only row `attended` counts
   *  registrations rather than people seen, and
   *  `show_rate` is null — not because nobody said yes, but because nobody was counted.
   *  Anything rendering these numbers has to say which it is showing. */
  attendance_evidence: string
}

export interface Overview {
  events: EventTurnout[]
  lifecycle_mix: Record<string, number>
  /** People analytics could SCORE — everyone with at least one attendance record.
   *  See `Status.people_in_store`. */
  people_scored: number
}

export interface Person {
  member_id: string
  name: string | null
  email: string | null
  /** `null` means no history yet — NOT zero. The two must render differently. */
  score: number | null
  lifecycle: string
  events_attended: number
  /** How many of `events_attended` came from an event that measured no attendance. A
   *  member with three attendances, one unmeasured, must not render identically to one
   *  who walked through three doors. */
  unmeasured_attendances: number
  no_shows: number
  opportunities: number
  attendance_rate: number
  first_seen_at: string
  last_attended_at: string | null
}

export interface TimelineEntry {
  event_id: string
  title: string
  starts_at: string
  /** `null` means the member has no record for an event that happened in their era.
   *  Read through the event's evidence, so on a registration-only event a member who
   *  said yes reads `'attended'` here — matching what the roster counts. */
  status: string | null
  attendance_evidence: string
}

export interface Cohort {
  month: string
  size: number
  /** One entry per month from the cohort's own. `null` = the question could not be asked. */
  attended: (number | null)[]
}

/** One event's room, and how much of it came back. `returned` is null for the most
 *  recent event — it has had nothing to return to, which is not the same as nobody
 *  returning, and rendering it as 0 reports a collapse on every fresh import. */
export interface EventCohort {
  event_id: string
  title: string
  starts_at: string
  attended: number
  returned: number | null
  return_rate: number | null
  attendance_evidence: string
}

export interface Retention {
  months: string[]
  /** The months an event actually happened in. `months` minus this is the set of
   *  "no event" cells — one of the three reasons a cell is blank. */
  active_months: string[]
  /** The current month, when it is on the grid and has not finished. Its cells are
   *  blank for a third reason — not "no event" and not "beyond the grid", but "not
   *  counted yet". Without it the legend calls this month's
   *  blanks "no event", which is a different and wrong story. */
  in_progress_month: string | null
  cohorts: Cohort[]
  /** Per-EVENT cohorts, beside the per-month grid rather than replacing it. A grid row is
   *  the first-timers of a month; a row here is the whole room at one event. On a young
   *  calendar the grid is mostly blanks and this is the readable one. */
  event_cohorts: EventCohort[]
}

/** `GET /api/events` — every event and what it measured, newest first. Independent of
 *  the thresholds on purpose: Settings lists these, and Settings is the way back from a
 *  threshold set that will not validate. */
export interface StoredEvent {
  event_id: string
  title: string
  starts_at: string
  attendance_evidence: string
}

export interface PendingEvent {
  luma_event_id: string
  inferred_title: string | null
  guests: number
  sources: string[]
  reason: string
}

/** `GET /api/import/pending` — what earlier reads could not name, from the STORE, so
 *  it survives the pass that produced it. `folder` is the last folder an import read. */
export interface PendingList {
  pending: PendingEvent[]
  folder: string | null
}

export interface ImportReport {
  files_read?: number
  files_skipped?: string[]
  events: number
  members: number
  attendances: number
  pending?: PendingEvent[]
  warnings: string[]
}

export interface Thresholds {
  regular_min_events: number
  champion_min_events: number
  champion_min_rate: number
  lapsed_after_days: number
  /** Why the STORED set could not be used, or null. When non-null the four values
   *  above are the defaults, not the organizer's — this endpoint answers with them
   *  rather than failing, because it is the only way back from an unusable set
   *. Anything rendering these must say which it is showing. */
  stored_is_unusable: string | null
}
