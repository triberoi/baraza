"""The four numbers that decide what a member is called.

Organizer-set, because a weekly meetup and a quarterly conference disagree about what
"regular" means. The defaults are round starting points, not a calibration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    """Organizer-set boundaries between the lifecycle labels.

    :param regular_min_events: attendances at which a member stops being a first-timer.
        At least 2.
    :param champion_min_events: attendances a champion needs. A champion must clear this
        and ``champion_min_rate``.
    :param champion_min_rate: share of available events a champion attends, in ``(0, 1]``.
    :param lapsed_after_days: days without attending before a member is lapsed. Also the
        recency window used by the score, so raising it moves both.
    """

    regular_min_events: int = 2
    champion_min_events: int = 4
    champion_min_rate: float = 0.5
    lapsed_after_days: int = 90

    #: `timedelta`'s ceiling. `lifecycle_label` builds one from this field, so a larger
    #: value is an `OverflowError` on every read of the store it was saved into.
    MAX_LAPSED_AFTER_DAYS = 999_999_999

    def __post_init__(self) -> None:
        # A float passes every comparison below and then moves a boundary silently, and
        # `Store.thresholds()` rebuilds this from JSON, so a hand-edited store reaches
        # here without passing the API's schema. `bool` is an `int` subclass.
        for field_name in ("regular_min_events", "champion_min_events", "lapsed_after_days"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{field_name} must be a whole number of events or days, got {value!r}")
        if not isinstance(self.champion_min_rate, int | float) or isinstance(self.champion_min_rate, bool):
            raise ValueError(f"champion_min_rate must be a number, got {self.champion_min_rate!r}")
        if self.regular_min_events < 2:
            raise ValueError("regular_min_events must be at least 2 — at 1 no member could be a first-timer")
        if self.champion_min_events < self.regular_min_events:
            raise ValueError(
                f"champion_min_events ({self.champion_min_events}) must be at least regular_min_events "
                f"({self.regular_min_events}) — otherwise the champion test is reached before the regular one"
            )
        if not 0.0 < self.champion_min_rate <= 1.0:
            raise ValueError(f"champion_min_rate must be in (0, 1], got {self.champion_min_rate}")
        if self.lapsed_after_days < 1:
            raise ValueError(f"lapsed_after_days must be at least 1, got {self.lapsed_after_days}")
        if self.lapsed_after_days > self.MAX_LAPSED_AFTER_DAYS:
            raise ValueError(
                f"lapsed_after_days must be at most {self.MAX_LAPSED_AFTER_DAYS}, got {self.lapsed_after_days}"
            )


__all__ = ["Thresholds"]
