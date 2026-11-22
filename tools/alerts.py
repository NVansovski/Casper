"""Timers and alarms with a background scheduler.

The server cannot play sound on the client's speakers, so fired timers and alarms are put
into a queue. The client polls ``GET /alerts`` and plays the signal locally.
Timers live in memory (they do not survive a server restart); alarms are stored in
``data.json``.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, tzinfo
from functools import lru_cache
from typing import Any, Callable
from zoneinfo import ZoneInfo

from config import get_settings
from tools.data_store import JsonDataStore, get_data_store

logger = logging.getLogger(__name__)

TICK_S = 1.0
ALERT_TTL_S = 600  # an alert nobody picked up for 10 minutes is dropped
REPEAT_MODES = ("once", "daily", "weekdays")

Json = dict[str, Any]


def format_duration(seconds: int) -> str:
    """German spoken duration, e.g. '1 Stunde 5 Minuten'."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} {'Stunde' if hours == 1 else 'Stunden'}")
    if minutes:
        parts.append(f"{minutes} {'Minute' if minutes == 1 else 'Minuten'}")
    if secs:
        parts.append(f"{secs} {'Sekunde' if secs == 1 else 'Sekunden'}")
    return " ".join(parts) or "0 Sekunden"


def next_occurrence(hour: int, minute: int, repeat: str, after: datetime) -> datetime:
    """First moment after ``after`` at ``hour:minute`` that matches the repeat mode."""
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    if repeat == "weekdays":
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    return candidate


class AlertService:
    """Keeps timers and alarms, fires them on time and hands them to the client."""

    def __init__(
        self,
        store: JsonDataStore,
        timezone: tzinfo,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._timezone = timezone
        self._clock = clock
        self._lock = threading.RLock()
        self._timers: dict[str, Json] = {}
        self._alarms: list[Json] = []
        self._queue: deque[Json] = deque()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._load_alarms()

    # ------------------------------------------------------------------ public API
    def start(self) -> None:
        """Start the scheduler thread (idempotent)."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="casper-alerts", daemon=True)
                self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def add_timer(self, seconds: int, label: str) -> Json:
        timer = {"id": uuid.uuid4().hex[:6], "label": label, "seconds": seconds, "fires_at": self._clock() + seconds}
        with self._lock:
            self._timers[timer["id"]] = timer
        return self._describe_timer(timer)

    def add_alarm(self, hour: int, minute: int, label: str, repeat: str) -> Json:
        now = datetime.now(self._timezone)
        alarm = {
            "id": uuid.uuid4().hex[:6],
            "time": f"{hour:02d}:{minute:02d}",
            "label": label,
            "repeat": repeat,
            "next_fire": next_occurrence(hour, minute, repeat, now).isoformat(),
        }
        with self._lock:
            self._alarms.append(alarm)
            self._save_alarms()
        return self._describe_alarm(alarm)

    def list_all(self) -> Json:
        with self._lock:
            timers = sorted(self._timers.values(), key=lambda t: t["fires_at"])
            alarms = sorted(self._alarms, key=lambda a: a["next_fire"])
            return {
                "timers": [self._describe_timer(t) for t in timers],
                "alarms": [self._describe_alarm(a) for a in alarms],
            }

    def cancel(self, query: str, kind: str = "any") -> list[str]:
        """Cancel by id, by label (case-insensitive substring), or ``"all"``. Returns descriptions."""
        needle = query.strip().lower()
        removed: list[str] = []
        with self._lock:
            if kind in ("any", "timer"):
                for timer_id, timer in list(self._timers.items()):
                    if self._matches(needle, timer_id, timer["label"]):
                        del self._timers[timer_id]
                        removed.append(f"Timer {timer['label'] or format_duration(timer['seconds'])}")
            if kind in ("any", "alarm"):
                kept = []
                for alarm in self._alarms:
                    if self._matches(needle, alarm["id"], alarm["label"] or alarm["time"]):
                        removed.append(f"Wecker {alarm['time']} Uhr")
                    else:
                        kept.append(alarm)
                if len(kept) != len(self._alarms):
                    self._alarms = kept
                    self._save_alarms()
        return removed

    def pop_alerts(self) -> list[Json]:
        """Return and clear all fired alerts that are still fresh."""
        now = self._clock()
        with self._lock:
            alerts = [a for a in self._queue if now - a["created"] <= ALERT_TTL_S]
            self._queue.clear()
        return [{"id": a["id"], "kind": a["kind"], "message": a["message"]} for a in alerts]

    # ------------------------------------------------------------------ scheduler
    def tick(self) -> None:
        """Fire everything that is due. Called once a second by the scheduler thread."""
        now = self._clock()
        now_dt = datetime.now(self._timezone)
        with self._lock:
            for timer_id, timer in list(self._timers.items()):
                if timer["fires_at"] <= now:
                    del self._timers[timer_id]
                    label = f" für {timer['label']}" if timer["label"] else ""
                    self._fire("timer", f"Dein Timer{label} ist abgelaufen.")
            changed = False
            for alarm in list(self._alarms):
                if datetime.fromisoformat(alarm["next_fire"]) <= now_dt:
                    hour, minute = (int(part) for part in alarm["time"].split(":"))
                    text = alarm["label"] or f"Es ist {alarm['time']} Uhr."
                    self._fire("alarm", f"Wecker: {text}")
                    if alarm["repeat"] == "once":
                        self._alarms.remove(alarm)
                    else:
                        alarm["next_fire"] = next_occurrence(hour, minute, alarm["repeat"], now_dt).isoformat()
                    changed = True
            if changed:
                self._save_alarms()

    def _run(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception:
                logger.exception("Alert scheduler tick failed")

    def _fire(self, kind: str, message: str) -> None:
        logger.info("Alert fired: %s", message)
        self._queue.append({"id": uuid.uuid4().hex[:8], "kind": kind, "message": message, "created": self._clock()})

    # ------------------------------------------------------------------ persistence and formatting
    def _load_alarms(self) -> None:
        stored: list[Json] = self._store.read(lambda data: [dict(a) for a in data.get("alarms", [])])
        now = datetime.now(self._timezone)
        for alarm in stored:
            if datetime.fromisoformat(alarm["next_fire"]) <= now:
                if alarm["repeat"] == "once":
                    logger.info("Dropping alarm %s that was missed while the server was off", alarm["time"])
                    continue
                hour, minute = (int(part) for part in alarm["time"].split(":"))
                alarm["next_fire"] = next_occurrence(hour, minute, alarm["repeat"], now).isoformat()
            self._alarms.append(alarm)

    def _save_alarms(self) -> None:
        snapshot = [dict(a) for a in self._alarms]
        self._store.write(lambda data: data.__setitem__("alarms", snapshot))

    @staticmethod
    def _matches(needle: str, item_id: str, label: str) -> bool:
        return needle in ("all", "alle") or needle == item_id.lower() or (bool(needle) and needle in str(label).lower())

    def _describe_timer(self, timer: Json) -> Json:
        remaining = max(0, int(timer["fires_at"] - self._clock()))
        return {
            "id": timer["id"],
            "label": timer["label"],
            "total": format_duration(timer["seconds"]),
            "remaining": format_duration(remaining),
        }

    def _describe_alarm(self, alarm: Json) -> Json:
        fire = datetime.fromisoformat(alarm["next_fire"]).astimezone(self._timezone)
        return {
            "id": alarm["id"],
            "time": alarm["time"],
            "label": alarm["label"],
            "repeat": alarm["repeat"],
            "next": fire.strftime("%Y-%m-%d %H:%M"),
        }


@lru_cache(maxsize=1)
def get_alert_service() -> AlertService:
    """The process-wide service. Created (and started) on first use."""
    service = AlertService(get_data_store(), ZoneInfo(get_settings().timezone))
    service.start()
    return service
