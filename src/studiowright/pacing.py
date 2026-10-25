"""Pace generation submits per profile, across every process that uses the profile.

Each submit reserves the next free slot in the shared store, so two CLI calls or an MCP
tool and a CLI call never submit back to back. After Flow reports unusual activity the
profile cools down before anything else is sent.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from dataclasses import dataclass

from studiowright.store import Store


def env_seconds(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    try:
        return float(value) if value else default
    except ValueError:
        return default


@dataclass(frozen=True)
class PaceSettings:
    #: Minimum time between two submits of the same profile.
    gap_s: float = 20.0
    #: Random extra delay added to every gap, so submits are not evenly spaced.
    jitter_s: float = 6.0
    #: Pause after Flow reports unusual activity.
    cooldown_s: float = 30 * 60

    @classmethod
    def for_upscales(cls) -> PaceSettings:
        """Upscaled downloads: Flow refused them with "unusual activity" after ~45 in 37 min,
        so they are spaced wider and pause for an hour after a refusal."""
        return cls(
            gap_s=env_seconds("STUDIOWRIGHT_UPSCALE_GAP_S", 60.0),
            jitter_s=env_seconds("STUDIOWRIGHT_UPSCALE_JITTER_S", 20.0),
            cooldown_s=env_seconds("STUDIOWRIGHT_UPSCALE_COOLDOWN_S", 60 * 60.0),
        )

    @classmethod
    def load(cls, store: Store, profile: str) -> PaceSettings:
        """The profile's saved pacing (``sw pace set``), with environment variables on top."""
        saved = saved_pace(store, profile)
        return cls(
            gap_s=env_seconds("STUDIOWRIGHT_GAP_S", saved.get("gap_s", cls.gap_s)),
            jitter_s=env_seconds("STUDIOWRIGHT_JITTER_S", saved.get("jitter_s", cls.jitter_s)),
            cooldown_s=env_seconds(
                "STUDIOWRIGHT_COOLDOWN_S", saved.get("cooldown_s", cls.cooldown_s)
            ),
        )


PACE_FIELDS = ("gap_s", "jitter_s", "cooldown_s")


def saved_pace(store: Store, profile: str) -> dict[str, float]:
    saved = json.loads(store.get_setting(f"pace.{profile}") or "{}")
    return {key: float(saved[key]) for key in PACE_FIELDS if key in saved}


def save_pace(store: Store, profile: str, values: dict[str, float | None]) -> dict[str, float]:
    """Keep the given values for the profile (None leaves a value as it is); the result is
    what is saved now. An empty ``values`` after a reset falls back to the defaults."""
    saved = saved_pace(store, profile)
    for key, value in values.items():
        if key not in PACE_FIELDS:
            raise ValueError(f"unknown pacing setting {key!r}")
        if value is not None:
            if value < 0:
                raise ValueError(f"{key} must not be negative")
            saved[key] = float(value)
    store.set_setting(f"pace.{profile}", json.dumps(saved))
    return saved


class CoolingDown(RuntimeError):
    """Flow asked this profile to slow down; nothing may be submitted yet."""

    def __init__(self, until: float) -> None:
        self.until = until
        minutes = max(1, round((until - time.time()) / 60))
        super().__init__(
            f"Flow reported unusual activity; this profile is cooling down for ~{minutes} min"
        )


class Governor:
    def __init__(self, store: Store, profile: str, settings: PaceSettings | None = None) -> None:
        self.store = store
        self.profile = profile
        self.settings = settings or PaceSettings.load(store, profile)

    def reserve(self, now: float | None = None) -> float:
        """Reserve the next submit slot and return how long to wait for it."""
        now = time.time() if now is None else now
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT next_free, cooldown_until FROM pacing WHERE profile = ?", (self.profile,)
            ).fetchone()
            next_free, cooldown_until = (row["next_free"], row["cooldown_until"]) if row else (0, 0)
            if cooldown_until > now:
                raise CoolingDown(cooldown_until)
            slot = max(now, next_free)
            following = slot + self.settings.gap_s + random.uniform(0, self.settings.jitter_s)
            db.execute(
                "INSERT INTO pacing (profile, next_free) VALUES (?, ?) "
                "ON CONFLICT (profile) DO UPDATE SET next_free = excluded.next_free",
                (self.profile, following),
            )
        return slot - now

    async def wait_turn(self) -> float:
        """Sleep until this process may submit; returns the time waited."""
        delay = self.reserve()
        if delay > 0:
            await asyncio.sleep(delay)
        return delay

    def report_unusual_activity(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        until = now + self.settings.cooldown_s
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO pacing (profile, cooldown_until, refusals) VALUES (?, ?, 1) "
                "ON CONFLICT (profile) DO UPDATE SET cooldown_until = excluded.cooldown_until, "
                "refusals = refusals + 1",
                (self.profile, until),
            )
        return until

    def status(self) -> dict[str, float]:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM pacing WHERE profile = ?", (self.profile,)).fetchone()
        if not row:
            return {"next_free": 0.0, "cooldown_until": 0.0, "refusals": 0}
        return {
            "next_free": row["next_free"],
            "cooldown_until": row["cooldown_until"],
            "refusals": row["refusals"],
        }
