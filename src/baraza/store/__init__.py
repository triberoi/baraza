"""The local artifact: one SQLite file. Facts only — scores and labels are computed on
read.
"""

from __future__ import annotations

from baraza.store.sqlite import (
    SCHEMA_VERSION,
    Store,
    StoredAttendance,
    StoredEvent,
    StoredMember,
    StoreError,
    StoreNeedsUpgrade,
    StoreNotBaraza,
    StoreNotFound,
    StoreUnreadable,
    StoreVersionError,
    connect,
    open_store,
)

__all__ = [
    "SCHEMA_VERSION",
    "Store",
    "StoreError",
    "StoreNeedsUpgrade",
    "StoreNotBaraza",
    "StoreNotFound",
    "StoreUnreadable",
    "StoreVersionError",
    "StoredAttendance",
    "StoredEvent",
    "StoredMember",
    "connect",
    "open_store",
]
