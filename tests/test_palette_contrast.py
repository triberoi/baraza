"""Every chart colour must carry its own label at 4.5:1.

Scoring a fill against the PAGE only asks whether the mark is visible. What a reader has
to make out is the text ON the fill, which is what this checks.

Read out of the TypeScript source, never a copy of the values — a second list of hex codes
would drift from the one the browser paints, and this file would become a test of itself.

In the Python suite because the front end has no test runner, and adding one to a
published package is a tooling decision rather than a fix.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

VIZ_TS = Path(__file__).parents[1] / "ui" / "src" / "viz.ts"

#: WCAG 2.1 AA for body text. The labels on these fills are small text, so 3:1 (the
#: large-text allowance) is not the applicable floor.
AA_NORMAL_TEXT = 4.5


def _luminance(hex_colour: str) -> float:
    """Relative luminance, per WCAG 2.1."""
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    first, second = _luminance(a), _luminance(b)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _source() -> str:
    return VIZ_TS.read_text(encoding="utf-8")


def _hexes(block: str) -> list[str]:
    return [m.upper() for m in re.findall(r"#[0-9A-Fa-f]{6}", block)]


def _named_block(name: str) -> str:
    match = re.search(rf"export const {name}[^=]*=\s*(\{{.*?\}}|\[.*?\])", _source(), re.S)
    assert match is not None, f"could not find {name} in viz.ts — did it move or change shape?"
    return match.group(1)


def _declared(name: str) -> str:
    """A single declared hex constant, read from the source."""
    match = re.search(rf"export const {name} = '(#[0-9A-Fa-f]{{6}})'", _source())
    assert match is not None, f"{name} must be declared in viz.ts for this check to mean anything"
    return match.group(1).upper()


def _ink_for(fill: str) -> str:
    """What ``inkOn`` will actually put on this fill, read from the same source it is."""
    match = re.search(r"const DARK_INK_FILLS = new Set\(\[(.*?)\]\)", _source(), re.S)
    assert match is not None, "could not find DARK_INK_FILLS in viz.ts — did inkOn change shape?"
    return _declared("INK_DARK") if fill.upper() in set(_hexes(match.group(1))) else _declared("INK_WHITE")


def _no_text_fills() -> set[str]:
    """Fills viz.ts declares never hold text — exempt from the LABEL rule, held to the
    mark-visibility rules in their own test below. The full-sunset reskin is what
    created the category: marigold cannot pass 4.5:1 for any ink, and it is admitted
    only because the turnout chart labels beside its bars, never on them."""
    match = re.search(r"export const NO_TEXT_FILLS = new Set\(\[(.*?)\]\)", _source(), re.S)
    return set(_hexes(match.group(1))) if match else set()


def _all_fills() -> list[tuple[str, str]]:
    fills = [("LIFECYCLE_FILL", h) for h in _hexes(_named_block("LIFECYCLE_FILL"))]
    fills += [("RETENTION_RAMP", h) for h in _hexes(_named_block("RETENTION_RAMP"))]
    fills += [("TURNOUT_FILL", h) for h in _hexes(_named_block("TURNOUT_FILL"))]
    # The unknown-lifecycle fallback renders a chip like any other fill, so it is held
    # to the same bar. Its predecessor was an inline light gray that would have carried
    # white text at 2.4:1 had an unknown stage ever reached it.
    fills.append(("FALLBACK_FILL", _declared("FALLBACK_FILL")))
    return fills


def _labelled_fills() -> list[tuple[str, str]]:
    exempt = _no_text_fills()
    return [(source, fill) for source, fill in _all_fills() if fill not in exempt]


def test_every_fill_carries_its_own_label() -> None:
    """The check that was missing. Each fill is scored against the ink the app will
    genuinely put on it, not against the page. `NO_TEXT_FILLS` are exempt — and only
    from THIS rule; their own tests below hold them to mark visibility and keep them
    out of every surface that does carry text."""
    failures = []
    for source, fill in _labelled_fills():
        ink = _ink_for(fill)
        ratio = contrast(fill, ink)
        if ratio < AA_NORMAL_TEXT:
            failures.append(f"{source} {fill} with ink {ink}: {ratio:.2f}:1")
    assert not failures, "text on these fills is not readable:\n  " + "\n  ".join(failures)


def test_the_palette_avoids_the_band_where_no_ink_works() -> None:
    """Why the palette moved rather than the ink rule.

    Against charcoal a fill has to be light; against white it has to be dark. Between
    those is a band where NEITHER reaches 4.5:1 — and an evenly-spaced five-step ramp
    from light to dark lands its middle steps right in it, which is exactly what
    happened. Asserted as a property so a future "let us soften the middle" puts this
    back in front of whoever tries it.
    """
    ink_dark = re.search(r"export const INK_DARK = '(#[0-9A-Fa-f]{6})'", _source())
    assert ink_dark is not None
    for source, fill in _labelled_fills():
        best = max(contrast(fill, "#FFFFFF"), contrast(fill, ink_dark.group(1)))
        assert best >= AA_NORMAL_TEXT, (
            f"{source} {fill} is in the dead band — best possible ink gives {best:.2f}:1, "
            "so no ink choice can rescue it and the colour itself has to change"
        )


def test_no_text_fills_stay_off_every_labelled_surface() -> None:
    """The exemption above is safe only while the exempted fills genuinely never sit
    under text. The lifecycle stack and the retention grid paint their values ON the
    fill, and the chip fallback carries a word — so a NO_TEXT_FILLS member appearing in
    any of them is the unreadable-label defect coming back through the side door."""
    exempt = _no_text_fills()
    assert exempt, "NO_TEXT_FILLS is empty or missing — if the category was removed, remove this test with it"
    for name in ("LIFECYCLE_FILL", "RETENTION_RAMP"):
        overlap = exempt & set(_hexes(_named_block(name)))
        assert not overlap, f"{sorted(overlap)} are declared no-text but {name} puts text on its fills"
    assert _declared("FALLBACK_FILL") not in exempt, "the chip fallback carries a word"


def test_no_text_fills_are_still_visible_marks() -> None:
    """What replaces the label rule for an exempted fill: WCAG 1.4.11's 3:1 non-text
    floor against the adjacent series it must be told apart from, and a 2:1 floor
    against the page so the mark cannot fade to a tint (the light lifecycle step ships
    at 2.1:1 against the page under the same reading — the bars also carry their counts
    beside them, so color is never the only carrier)."""
    turnout = _hexes(_named_block("TURNOUT_FILL"))
    for fill in _no_text_fills():
        assert contrast(fill, "#FFFFFF") >= 2.0, f"{fill} is fading into the page"
        neighbours = [other for other in turnout if other != fill]
        assert neighbours and all(contrast(fill, other) >= 3.0 for other in neighbours), (
            f"{fill} does not clear 3:1 against the series beside it"
        )


def test_the_retention_ramp_is_still_monotone() -> None:
    """Fixing contrast must not cost the ramp its order: it encodes a rate, and a step
    that is lighter than the one before it makes a higher rate look like a lower one."""
    ramp = _hexes(_named_block("RETENTION_RAMP"))
    assert len(ramp) == 5
    lums = [_luminance(step) for step in ramp]
    assert lums == sorted(lums, reverse=True), f"the ramp is not light-to-dark: {ramp}"


def test_adjacent_ramp_steps_are_still_tellable_apart() -> None:
    """A ramp all of whose steps clear contrast but which reads as one block is not a
    ramp. 1.2:1 between neighbours is the floor the original validator used."""
    ramp = _hexes(_named_block("RETENTION_RAMP"))
    for earlier, later in zip(ramp, ramp[1:], strict=False):
        assert contrast(earlier, later) >= 1.2, f"{earlier} and {later} are too close to distinguish"


@pytest.mark.parametrize("bad", ["#7F8FA6", "#8695A9", "#697A92"])
def test_the_three_colours_the_review_found_would_still_fail(bad: str) -> None:
    """The guard proven against the actual defect rather than only against the fix.

    These are the three the review named. Each is in the dead band, so this test would
    have gone red on the palette as it shipped — which is what makes the two tests above
    guards rather than a description of whatever the palette happens to be.
    """
    best = max(contrast(bad, "#FFFFFF"), contrast(bad, "#30435B"))
    assert best < AA_NORMAL_TEXT, f"{bad} was supposed to be unreadable; it scores {best:.2f}:1"
    assert bad not in [fill for _, fill in _all_fills()], f"{bad} is back in the palette"


# --- the UI chrome, from tokens.css --------------------------------------------------
# The 2026-08-16 accessibility audit found the chart fills thoroughly tested and the
# CHROME not tested at all — and failing: supporting text was #9CAABD at 2.36:1 on
# white, on every screen. These tests close that hole the same way the chart tests
# did: read the values out of the stylesheet the browser actually loads, never a copy.

TOKENS_CSS = Path(__file__).parents[1] / "ui" / "src" / "tokens.css"


def _tokens() -> dict[str, str]:
    """Every ``--name: value`` in tokens.css, with one level of var() indirection
    resolved — which is all the file uses."""
    text = TOKENS_CSS.read_text(encoding="utf-8")
    raw = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", text))
    resolved: dict[str, str] = {}
    for name, value in raw.items():
        value = value.strip()
        ref = re.fullmatch(r"var\((--[\w-]+)\)", value)
        resolved[name] = raw.get(ref.group(1), "").strip() if ref else value
    return resolved


def _token_hex(tokens: dict[str, str], name: str) -> str:
    value = tokens.get(name, "")
    match = re.fullmatch(r"#[0-9A-Fa-f]{6}", value)
    assert match is not None, f"{name} did not resolve to a hex color (got {value!r})"
    return value.upper()


@pytest.mark.parametrize(
    ("ink", "ground"),
    [
        ("--fg-default", "--bg-page"),
        ("--fg-default", "--bg-soft"),
        # THE pair that failed the audit: muted text is captions, labels, legends and
        # explainer copy — most of the secondary text on every screen.
        ("--fg-muted", "--bg-page"),
        ("--fg-muted", "--bg-soft"),
        ("--fg-on-dark", "--bg-dark"),
        ("--fg-on-dark-mut", "--bg-dark"),
        # Buttons and the save/problem confirmations: colored text or white-on-color.
        ("--fg-on-dark", "--fg-interactive"),
        ("--fg-interactive", "--bg-page"),
        ("--tr-success", "--bg-page"),
        # The full-sunset chrome: warm labels on both of their grounds, and muted
        # text on the tile gradient's warmest point.
        ("--fg-label", "--bg-page"),
        ("--fg-label", "--bz-dawn"),
        ("--fg-muted", "--bz-dawn"),
    ],
)
def test_chrome_text_clears_aa_on_its_ground(ink: str, ground: str) -> None:
    tokens = _tokens()
    ratio = contrast(_token_hex(tokens, ink), _token_hex(tokens, ground))
    assert ratio >= AA_NORMAL_TEXT, f"{ink} on {ground}: {ratio:.2f}:1 — below AA's 4.5:1"


def test_the_gray_that_failed_the_audit_is_not_muted_text() -> None:
    """Proven against the actual defect: #9CAABD as --fg-muted is the exact
    configuration every screen shipped with, and it must score below AA here — that is
    what makes the parametrized test above a guard rather than a description."""
    tokens = _tokens()
    assert contrast("#9CAABD", _token_hex(tokens, "--bg-page")) < AA_NORMAL_TEXT
    assert _token_hex(tokens, "--fg-muted") != "#9CAABD"
