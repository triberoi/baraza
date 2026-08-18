/**
 * Settings — the organizer's lifecycle boundaries, and the way out of an unusable set.
 *
 * The API for this existed before the screen did: `GET /thresholds` was written to
 * "arrive populated so the screen can say what happened", and error copy elsewhere told
 * the organizer to "Open Settings" — while the app had no Settings to open. This is
 * that screen.
 *
 * Two rules carried over from the API's design:
 *
 * 1. **It never refuses to render.** When the stored set will not validate (a file
 *    edited by hand, or written by looser rules), the form arrives filled with the
 *    defaults and says whose numbers it is showing — because this form is the only way
 *    back, and failing here would lock the organizer out of the fix by the bug.
 * 2. **Validation lives server-side.** A set that is individually valid but incoherent
 *    (a champion floor below the regular one) comes back as a 422 with the reason, and
 *    that sentence is shown as-is rather than re-derived here.
 */
import { useEffect, useState } from 'react'

import { ApiError, get, put, type Thresholds } from '../api'

/** The form's working copy: strings, because an input holds text and "" mid-edit is
 *  not 0. Converted (and refused) only on save. */
interface Draft {
  regular_min_events: string
  champion_min_events: string
  champion_min_rate: string
  lapsed_after_days: string
}

function toDraft(t: Thresholds): Draft {
  return {
    regular_min_events: String(t.regular_min_events),
    champion_min_events: String(t.champion_min_events),
    // Stored as a rate in (0, 1]; edited as a percentage, because "50" reads as a
    // share of events and "0.5" reads as half an event.
    champion_min_rate: String(Math.round(t.champion_min_rate * 100)),
    lapsed_after_days: String(t.lapsed_after_days),
  }
}

export function SettingsScreen() {
  const [draft, setDraft] = useState<Draft | null>(null)
  const [storedProblem, setStoredProblem] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let live = true
    get<Thresholds>('/api/thresholds')
      .then((t) => {
        if (!live) return
        setDraft(toDraft(t))
        setStoredProblem(t.stored_is_unusable)
      })
      .catch((error: unknown) => {
        if (live) setProblem(error instanceof ApiError ? error.message : String(error))
      })
    return () => {
      live = false
    }
  }, [])

  async function save() {
    if (draft === null || saving) return
    setSaving(true)
    setProblem(null)
    setSaved(false)
    try {
      const body = {
        regular_min_events: Number(draft.regular_min_events),
        champion_min_events: Number(draft.champion_min_events),
        champion_min_rate: Number(draft.champion_min_rate) / 100,
        lapsed_after_days: Number(draft.lapsed_after_days),
      }
      const stored = await put<Thresholds>('/api/thresholds', body)
      setDraft(toDraft(stored))
      // A good set on disk is what retires the warning; saying so beats leaving a
      // stale alarm above a form that just fixed it.
      setStoredProblem(null)
      setSaved(true)
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : String(error))
    } finally {
      setSaving(false)
    }
  }

  if (draft === null && problem === null) return <p className="muted">Working it out…</p>

  return (
    <>
      <h1>Settings</h1>
      <p className="lede">
        Where the lifecycle lines are drawn. Change one and every screen re-labels itself on its
        next load — nothing is cached, and nothing has to be re-imported.
      </p>

      {storedProblem && (
        <div className="notice notice--problem" role="alert">
          <h3>The stored settings could not be used</h3>
          <p>{storedProblem}</p>
          <p className="muted">
            The numbers below are the defaults, not yours. Saving a good set fixes this.
          </p>
        </div>
      )}

      {draft && (
        <form
          className="settings"
          onSubmit={(e) => {
            e.preventDefault()
            void save()
          }}
        >
          <Field
            label="Regular after"
            unit="attendances"
            hint="At this many attendances someone stops being a first-timer. At least 2."
            value={draft.regular_min_events}
            onChange={(v) => setDraft({ ...draft, regular_min_events: v })}
          />
          <Field
            label="Champion after"
            unit="attendances"
            hint="A champion needs this many attendances, and the share below. Never lower than the regular line."
            value={draft.champion_min_events}
            onChange={(v) => setDraft({ ...draft, champion_min_events: v })}
          />
          <Field
            label="Champion share"
            unit="% of their events"
            hint="Of the events since they first appeared, the share a champion attends."
            value={draft.champion_min_rate}
            onChange={(v) => setDraft({ ...draft, champion_min_rate: v })}
          />
          <Field
            label="Lapsed after"
            unit="days"
            hint="Days without attending before someone counts as lapsed. Also the recency window the score uses, so raising it moves both."
            value={draft.lapsed_after_days}
            onChange={(v) => setDraft({ ...draft, lapsed_after_days: v })}
          />
          <div className="settings__actions">
            <button className="button" type="submit" disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </button>
            {saved && !problem && <span className="saved">Saved.</span>}
          </div>
        </form>
      )}

      {problem && (
        <div className="notice notice--problem" role="alert">
          <h3>That did not work</h3>
          <p>{problem}</p>
        </div>
      )}
    </>
  )
}

function Field({
  label,
  unit,
  hint,
  value,
  onChange,
}: {
  label: string
  unit: string
  hint: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <label className="field settings__field">
      <span className="label">{label}</span>
      <span className="settings__control">
        <input
          type="number"
          inputMode="numeric"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
        <span className="muted">{unit}</span>
      </span>
      <span className="muted settings__hint">{hint}</span>
    </label>
  )
}
