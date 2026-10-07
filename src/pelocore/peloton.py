"""Peloton data access layer wrapping ``pylotoncycle``.

Exposes a small protocol-friendly surface so the sync engine can be tested
with fakes: ``workouts_since(days)`` and ``performance(workout_id)``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from pylotoncycle import PylotonCycle

PAGE_SIZE = 100
MAX_PAGES = 10

MILE_IN_METERS = 1609.344
MPH_TO_MPS = 0.44704


@dataclass(frozen=True)
class PelotonWorkout:
    id: str
    status: str
    fitness_discipline: str
    is_outdoor: bool
    start_time: int  # epoch seconds
    end_time: int | None
    title: str
    instructor: str | None
    total_work: float | None = None
    ftp: float | None = None


@dataclass(frozen=True)
class PerformanceSample:
    """One second of performance data, normalized to FIT units."""

    offset: int  # seconds from workout start
    power: float | None = None
    cadence: float | None = None
    heart_rate: float | None = None
    speed_ms: float | None = None
    distance_m: float | None = None
    calories: float | None = None
    resistance: float | None = None


@dataclass(frozen=True)
class LocationPoint:
    offset: int
    lat: float
    lon: float
    distance_m: float | None = None
    heart_rate: float | None = None
    speed_ms: float | None = None


@dataclass(frozen=True)
class WorkoutPerformance:
    duration_s: int
    samples: list[PerformanceSample]
    locations: list[LocationPoint]


class PelotonSource(Protocol):
    def workouts_since(self, days: int) -> list[PelotonWorkout]: ...
    def performance(self, workout_id: str) -> WorkoutPerformance: ...


def parse_performance(pg: dict[str, Any]) -> WorkoutPerformance:
    """Normalize a Peloton ``performance_graph`` payload.

    Indoor workouts carry a ``metrics`` array aligned with
    ``seconds_since_pedaling_start``. Outdoor workouts carry ``location_data``
    whose coordinates embed per-point metrics.
    """
    duration = int(pg.get("duration") or 0)
    locations = _parse_locations(pg.get("location_data") or [])
    samples: list[PerformanceSample] = []
    if locations:
        samples = [_sample_from_location(loc) for loc in locations]
    else:
        samples = _parse_metric_samples(pg)
    return WorkoutPerformance(duration_s=duration, samples=samples, locations=locations)


def _parse_locations(location_data: list[Any]) -> list[LocationPoint]:
    points: list[LocationPoint] = []
    for segment in location_data:
        if not isinstance(segment, dict):
            continue
        for coord in segment.get("coordinates", []) or []:
            point = _parse_location(coord)
            if point is not None:
                points.append(point)
    points.sort(key=lambda p: p.offset)
    return points


def _parse_location(coord: dict[str, Any]) -> LocationPoint | None:
    try:
        offset = int(coord["seconds_offset_from_start"])
        lat = float(coord["latitude"])
        lon = float(coord["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return LocationPoint(
        offset=offset,
        lat=lat,
        lon=lon,
        distance_m=_optional_miles_to_meters(coord, "distance"),
        heart_rate=_optional_number(coord, "heart_rate"),
        speed_ms=_optional_mph_to_ms(coord, "speed"),
    )


def _sample_from_location(loc: LocationPoint) -> PerformanceSample:
    return PerformanceSample(
        offset=loc.offset,
        heart_rate=loc.heart_rate,
        speed_ms=loc.speed_ms,
        distance_m=loc.distance_m,
    )


def _parse_metric_samples(pg: dict[str, Any]) -> list[PerformanceSample]:
    metrics: dict[str, list[float]] = {}
    for entry in pg.get("metrics") or []:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        values = entry.get("values")
        if isinstance(slug, str) and isinstance(values, list):
            metrics[slug] = values

    offsets = pg.get("seconds_since_pedaling_start")
    if isinstance(offsets, list) and offsets:
        index_pairs = [
            (idx, off)
            for idx, off in enumerate(offsets)
            if isinstance(off, int) and off >= 0
        ]
    else:
        longest = max((len(v) for v in metrics.values()), default=0)
        index_pairs = [(idx, idx) for idx in range(longest)]

    samples: list[PerformanceSample] = []
    for idx, offset in index_pairs:
        sample = PerformanceSample(
            offset=offset,
            power=_value(metrics, "output", idx),
            cadence=_value(metrics, "cadence", idx),
            heart_rate=_value(metrics, "heart_rate", idx),
            speed_ms=_convert(_value(metrics, "speed", idx), MPH_TO_MPS),
            distance_m=_convert(_value(metrics, "distance", idx), MILE_IN_METERS),
            calories=_value(metrics, "calories", idx),
            resistance=_value(metrics, "resistance", idx),
        )
        samples.append(sample)
    samples.sort(key=lambda s: s.offset)
    return samples


def _value(metrics: dict[str, list[float]], slug: str, idx: int) -> float | None:
    values = metrics.get(slug)
    if values is None or idx >= len(values):
        return None
    value = values[idx]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _convert(value: float | None, factor: float) -> float | None:
    return None if value is None else value * factor


def _optional_number(source: dict[str, Any], key: str) -> float | None:
    value = source.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_mph_to_ms(source: dict[str, Any], key: str) -> float | None:
    value = _optional_number(source, key)
    return None if value is None else value * MPH_TO_MPS


def _optional_miles_to_meters(source: dict[str, Any], key: str) -> float | None:
    value = _optional_number(source, key)
    return None if value is None else value * MILE_IN_METERS


class PylotonClient:
    """Concrete :class:`PelotonSource` backed by :class:`PylotonCycle`."""

    def __init__(
        self,
        *,
        username: str = "",
        password: str = "",
        refresh_token: str = "",
        timeout: float = 30.0,
        client: PylotonCycle | None = None,
    ):
        self._username = username
        self._password = password
        self._refresh_token = refresh_token
        self._timeout = timeout
        self._client: PylotonCycle | None = client

    def _session(self) -> PylotonCycle:
        if self._client is None:
            self._client = PylotonCycle(
                username=self._username,
                password=self._password,
                refresh_token=self._refresh_token or None,
            )
        return self._client

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self._session().s.get(url, timeout=self._timeout)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data

    def whoami(self) -> dict[str, str | int]:
        session = self._session()
        return {
            "username": str(getattr(session, "username", "") or ""),
            "user_id": str(getattr(session, "userid", "") or ""),
            "total_workouts": int(getattr(session, "total_workouts", 0) or 0),
        }

    def workouts_since(self, days: int) -> list[PelotonWorkout]:
        """Newest-first workouts newer than ``days`` ago (capped at MAX_PAGES)."""
        session = self._session()
        user_id = session.userid
        cutoff = time.time() - days * 86_400
        workouts: list[PelotonWorkout] = []
        for page in range(MAX_PAGES):
            url = (
                f"https://api.onepeloton.com/api/user/{user_id}/workouts"
                f"?sort_by=-created&page={page}&limit={PAGE_SIZE}"
            )
            payload = self._get_json(url)
            batch = payload.get("data") or []
            if not batch:
                break
            reached_cutoff = False
            for raw in batch:
                workout = _normalize_workout(raw)
                if workout is None:
                    continue
                if workout.start_time < cutoff:
                    reached_cutoff = True
                    break
                workouts.append(workout)
            if reached_cutoff or len(batch) < PAGE_SIZE:
                break
        return workouts

    def performance(self, workout_id: str) -> WorkoutPerformance:
        url = (
            "https://api.onepeloton.com/api/workout/"
            f"{workout_id}/performance_graph?every_n=1"
        )  # every_n=1 → second-by-second samples
        return parse_performance(self._get_json(url))


def _normalize_workout(raw: dict[str, Any]) -> PelotonWorkout | None:
    workout_id = raw.get("id")
    if not workout_id:
        return None
    ride = raw.get("ride") or {}
    title = ride.get("title") or raw.get("name") or raw.get("title") or f"Workout {workout_id}"
    instructor: str | None = None
    embedded_instructor = ride.get("instructor")
    if isinstance(embedded_instructor, dict):
        instructor = embedded_instructor.get("name")
    elif isinstance(embedded_instructor, str):
        instructor = embedded_instructor
    ftp = (raw.get("ftp") or {}).get("ftp") if isinstance(raw.get("ftp"), dict) else None
    return PelotonWorkout(
        id=str(workout_id),
        status=str(raw.get("status") or ""),
        fitness_discipline=str(raw.get("fitness_discipline") or ""),
        is_outdoor=bool(raw.get("is_outdoor", False)),
        start_time=int(raw.get("start_time") or 0),
        end_time=int(raw["end_time"]) if raw.get("end_time") is not None else None,
        title=str(title),
        instructor=instructor,
        total_work=_maybe_float(raw.get("total_work")),
        ftp=_maybe_float(ftp),
    )


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
