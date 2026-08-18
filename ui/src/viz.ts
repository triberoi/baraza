/**
 * The chart palette. Every value is measured, not chosen by eye.
 *
 * Contrast is scored against the TEXT ON the fill, not just against the page — a fill a
 * reader can see is not the same as one whose own label they can read.
 *
 * That constraint is what sets the ramps. Charcoal text needs a light fill (luminance
 * ≥ 0.418), white text needs a dark one (≤ 0.183), and between them is a band where no
 * ink reaches 4.5:1 at all. A five-step ramp drawn evenly from light to dark puts its
 * middle steps in that band, so the ramps live entirely below it, white ink throughout,
 * every step at 4.62:1 or better. `prospect` is the exception at #86C056 with charcoal
 * text (4.66:1), because the lifecycle bar needs a light first step to read as a ramp.
 *
 * The hue is olive at full chroma — the 2026-08-16 reskin, punched up the same day at
 * Mark's direction ("still looks dull and flat"): the first pass matched the muted
 * slate ladder's chroma as well as its luminance and inherited its flatness. Every
 * step still matches that validated ladder luminance for luminance, so contrast,
 * monotonicity and adjacency carried over by construction. The chroma costs colorblind
 * adjacent-step separation — worst adjacent ΔE 6.4 against the muted set's 8.1 —
 * which the validator allows only with secondary encoding: the lifecycle bar's direct
 * labels and the 2px segment gaps are that encoding, and the choice was made knowing
 * the trade.
 *
 * Nothing starts at the soft-panel sage: like its gray predecessor it sits near 1.1:1
 * against a white page, an invisible first step.
 *
 * Single light theme, deliberately. These ramps do not pass against a dark surface and
 * are not meant to.
 */

/**
 * Lifecycle, as an ordinal ramp — one hue getting stronger, because
 * prospect → first-timer → regular → champion is an **order**. Four hues would say
 * these are four unrelated things.
 *
 * `lapsed` is off the ramp and orange on purpose: baraza's brand rule is that orange
 * marks only what needs action (the role the parent brand gives red — baraza has no
 * red), and a member who stopped coming is the one row on this chart an organizer can
 * do something about. White text on it clears AA at 5.5:1.
 */
export const LIFECYCLE_FILL: Record<string, string> = {
  prospect: '#86C056',
  first_timer: '#567E35',
  regular: '#486A2D',
  champion: '#2A3E1A',
  lapsed: '#A84F2A',
}

/** Display order, matching the API's own. The bar must not re-order between runs. */
export const LIFECYCLE_ORDER = ['prospect', 'first_timer', 'regular', 'champion', 'lapsed'] as const

export const LIFECYCLE_LABEL: Record<string, string> = {
  prospect: 'Prospect',
  first_timer: 'First-timer',
  regular: 'Regular',
  champion: 'Champion',
  lapsed: 'Lapsed',
}

/**
 * Turnout: deep olive against marigold — the sunset against the bush, and the one
 * chart where a fill carries NO text (the counts sit beside the bars, the legend
 * swatches are aria-hidden with their words in ink). That is what admits marigold at
 * all: it can never pass 4.5:1 for a label (2.3:1 white, 4.4:1 charcoal), so it is in
 * `NO_TEXT_FILLS` and the contrast suite holds it to mark-visibility rules instead —
 * 3.4:1 against its neighbouring series, above the 3:1 non-text floor.
 */
export const TURNOUT_FILL = { returning: '#3D5A26', new: '#E89A3F' } as const

/**
 * Retention: five steps of one hue, light→dark, validated monotone.
 *
 * A cell with **no data gets no step** — see `retentionFill`. Painting "no event that
 * month" as the lightest step would say the cohort came back least that month, when
 * the truth is there was nothing to come back to.
 */
export const RETENTION_RAMP = ['#578036', '#496C2D', '#3D5A26', '#32491F', '#253717'] as const

export function retentionFill(rate: number | null): string | null {
  if (rate === null) return null
  const step = Math.min(RETENTION_RAMP.length - 1, Math.floor(rate * RETENTION_RAMP.length))
  return RETENTION_RAMP[step] ?? RETENTION_RAMP[0]
}

/** The one fill light enough to carry dark text. Everything else takes white. */
const DARK_INK_FILLS = new Set(['#86C056'])

/**
 * Fills that never hold text, exempt from the on-fill label rule and held to
 * mark-visibility rules instead (tests/test_palette_contrast.py). Marigold is the
 * inner sun of the print and earns its place as a warm data series exactly because
 * the turnout chart labels beside its bars, never on them. Putting a member of this
 * set anywhere text lands on the fill — the lifecycle stack, the retention grid, a
 * chip — reintroduces the unreadable-label defect the suite exists to prevent, and
 * the suite asserts the separation.
 */
export const NO_TEXT_FILLS = new Set(['#E89A3F'])

/**
 * What an UNKNOWN lifecycle key renders as — the compiler-required `??` fallbacks in
 * `charts.tsx` and `People.tsx` point here rather than at inline hexes, so the token
 * cannot fork from the palette. Dark, because `inkOn` puts white text on anything not
 * in `DARK_INK_FILLS`: the old inline fallback in the roster was a LIGHT gray, which
 * would have carried white text at 2.4:1 had an unknown stage ever reached it.
 */
export const FALLBACK_FILL = '#3D5A26'

// Exported although no TypeScript imports them: `tests/test_palette_contrast.py` reads
// them out of THIS SOURCE, so the contrast check scores against the ink the app really
// uses. A dead-code sweep that only looks at TS callers will call them unused.
export const INK_DARK = '#30435B'
export const INK_WHITE = '#FFFFFF'

/**
 * Ink on a filled mark, chosen so the TEXT clears 4.5:1 — which is the check that was
 * missing.
 *
 * The old rule was a hardcoded list of "the two darkest steps", and it was not the
 * failure: three fills were unreadable **whatever ink you put on them**, because their
 * luminance sat in a dead zone where neither white nor charcoal reaches 4.5:1. That is
 * why the palette moved rather than this function.
 */
export function inkOn(fill: string): string {
  return DARK_INK_FILLS.has(fill.toUpperCase()) ? INK_DARK : INK_WHITE
}

export const percent = (value: number): string => `${Math.round(value * 100)}%`

export function shortMonth(month: string): string {
  const [year, index] = month.split('-')
  const names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
  return `${names[Number(index) - 1] ?? month} ${year?.slice(2) ?? ''}`
}

export function shortDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}
