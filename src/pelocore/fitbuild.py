"""Build FIT activity files from Peloton workouts using ``fit_tool``.

Produces a complete Activity file: ``file_id``, timer start event, per-second
``record`` messages (or GPS points for outdoor workouts), one ``lap``,
timer stop event, a ``session`` and an ``activity`` message - the shape the
FIT SDK sample files use (session/lap carry ``event=LAP, event_type=STOP``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from fit_tool import FitFile, FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.exercise_title_message import ExerciseTitleMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.set_message import SetMessage
from fit_tool.profile.profile_type import Event, EventType, FileType, Manufacturer

from pelocore.peloton import ExerciseBlock, PelotonWorkout, WorkoutPerformance
from pelocore.sports import SportMapping, mapping_for, remap_discipline

#: Peloton performance data is imperial in the API; convert for FIT.
MPH_TO_MPS = 0.44704
MILE_IN_METERS = 1609.344

MAX_PROFILE_NAME_CHARS = 120


class FitBuildError(Exception):
    """Raised when a FIT file cannot be built or fails self-validation."""


@dataclass(frozen=True)
class FitBuildResult:
    data: bytes
    record_count: int
    sport_label: str
    sport: str
    sub_sport: str | None


def build_activity_fit(
    workout: PelotonWorkout,
    perf: WorkoutPerformance | None = None,
    *,
    plan: list[ExerciseBlock] | None = None,
    remaps: dict[str, str] | None = None,
) -> FitBuildResult:
    effective = remap_discipline(workout.fitness_discipline, remaps)
    mapping = mapping_for(effective, is_outdoor=workout.is_outdoor)
    start_ms = workout.start_time * 1000
    end_offset = _end_offset_s(workout, perf)
    end_ms = start_ms + end_offset * 1000

    builder = FitFileBuilder(auto_define=True, min_string_size=50)

    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.DEVELOPMENT
    file_id.product = 0
    file_id.serial_number = _serial_number(workout.id)
    file_id.time_created = end_ms
    builder.add(file_id)

    start_event = EventMessage()
    start_event.event = Event.TIMER
    start_event.event_type = EventType.START
    start_event.timestamp = start_ms
    builder.add(start_event)

    records = _record_messages(perf, start_ms)
    if not records:
        # The FIT FILE_TYPE conformance rules require at least one record in an
        # activity file; summary-only workouts (strength, yoga) get a single
        # timestamp-only record to anchor the session start.
        anchor = RecordMessage()
        anchor.timestamp = start_ms
        records = [anchor]
    for record in records:
        builder.add(record)

    if plan and not (perf and (perf.samples or perf.locations)):
        _add_strength_structure(builder, plan, start_ms)

    builder.add(_lap_message(mapping, workout, perf, start_ms, end_ms, end_offset))

    stop_event = EventMessage()
    stop_event.event = Event.TIMER
    stop_event.event_type = EventType.STOP
    stop_event.timestamp = end_ms
    builder.add(stop_event)

    builder.add(_session_message(mapping, workout, perf, start_ms, end_ms, end_offset))
    builder.add(_activity_message(end_ms, end_offset))

    data = builder.build_bytes()
    _self_check(data)
    sub = mapping.sub_sport.name if mapping.sub_sport is not None else None
    return FitBuildResult(
        data=data,
        record_count=len(records),
        sport_label=mapping.label,
        sport=mapping.sport.name,
        sub_sport=sub,
    )


# -- helpers ---------------------------------------------------------------


def _end_offset_s(workout: PelotonWorkout, perf: WorkoutPerformance | None) -> int:
    """Active duration in seconds; the performance graph wins over end_time."""
    if perf is not None and perf.duration_s:
        return perf.duration_s
    if perf is not None:
        offsets = [sample.offset for sample in perf.samples]
        offsets += [point.offset for point in perf.locations]
        if offsets:
            return max(offsets)
    if workout.end_time is not None and workout.end_time > workout.start_time:
        return workout.end_time - workout.start_time
    return 1


def _serial_number(workout_id: str) -> int:
    digest = hashlib.sha256(workout_id.encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) & 0xFFFFFFFF


def _record_messages(perf: WorkoutPerformance | None, start_ms: int) -> list[RecordMessage]:
    if perf is None:
        return []
    distance_series = _distance_series(perf)
    records: list[RecordMessage] = []
    if perf.locations:
        for point in perf.locations:
            record = RecordMessage()
            record.timestamp = start_ms + point.offset * 1000
            record.position_lat = point.lat
            record.position_long = point.lon
            if point.distance_m is not None:
                record.distance = point.distance_m
            if point.heart_rate is not None:
                record.heart_rate = int(point.heart_rate)
            if point.speed_ms is not None:
                record.speed = point.speed_ms
                record.enhanced_speed = point.speed_ms
            records.append(record)
        return records
    for index, sample in enumerate(perf.samples):
        record = RecordMessage()
        record.timestamp = start_ms + sample.offset * 1000
        if sample.power is not None:
            record.power = round(sample.power)
        if sample.cadence is not None:
            record.cadence = round(sample.cadence)
        if sample.heart_rate is not None:
            record.heart_rate = round(sample.heart_rate)
        if sample.speed_ms is not None:
            # Setting enhanced_speed too: the encoder expands speed → enhanced_speed
            # (field 73) at encode time, which would outgrow the auto-built
            # definition otherwise.
            record.speed = sample.speed_ms
            record.enhanced_speed = sample.speed_ms
        integrated = distance_series[index] if distance_series else None
        distance = sample.distance_m if sample.distance_m is not None else integrated
        if distance is not None:
            record.distance = distance
        if sample.resistance is not None:
            record.resistance = round(sample.resistance)
        records.append(record)
    return records


@dataclass(frozen=True)
class _Aggregates:
    total_distance: float | None = None
    total_calories: float | None = None
    total_output_kj: float | None = None
    avg_power: float | None = None
    max_power: float | None = None
    avg_heart_rate: float | None = None
    max_heart_rate: float | None = None
    avg_cadence: float | None = None
    max_cadence: float | None = None
    avg_speed: float | None = None
    max_speed: float | None = None


def _distance_series(perf: WorkoutPerformance | None) -> list[float | None] | None:
    """Cumulative per-sample distance in meters.

    Priority: Peloton distance slug (running) → integration of Peloton speed
    (cycling reports speed but no distance series). None when neither exists.
    """
    if perf is None or not perf.samples:
        return None
    samples = perf.samples
    if any(s.distance_m is not None for s in samples):
        return [s.distance_m for s in samples]
    if any(s.speed_ms is not None for s in samples):
        out: list[float | None] = []
        accumulated = 0.0
        previous = samples[0].offset
        for sample in samples:
            delta = sample.offset - previous
            previous = sample.offset
            if sample.speed_ms is not None and delta > 0:
                accumulated += sample.speed_ms * delta
            out.append(accumulated)
        return out
    return None


def _aggregate(perf: WorkoutPerformance | None) -> _Aggregates:
    if perf is None:
        return _Aggregates()
    samples = perf.samples
    distance_series = _distance_series(perf)
    total_distance = next(
        (s.distance_m for s in reversed(samples) if s.distance_m is not None), None
    )
    if total_distance is None and distance_series:
        total_distance = next((d for d in reversed(distance_series) if d is not None), None)
    if total_distance is None and perf.locations:
        total_distance = next(
            (p.distance_m for p in reversed(perf.locations) if p.distance_m is not None), None
        )
    total_calories = next(
        (s.calories for s in reversed(samples) if s.calories is not None), None
    )

    if perf.summary is not None:
        if perf.summary.total_distance_m is not None:
            total_distance = perf.summary.total_distance_m
        if perf.summary.total_calories is not None:
            total_calories = perf.summary.total_calories

    def stats(attr: str) -> tuple[float | None, float | None]:
        values = [getattr(s, attr) for s in samples if getattr(s, attr) is not None]
        if not values:
            return None, None
        return sum(values) / len(values), max(values)

    avg_power, max_power = stats("power")
    avg_heart_rate, max_heart_rate = stats("heart_rate")
    avg_cadence, max_cadence = stats("cadence")
    avg_speed, max_speed = stats("speed_ms")
    return _Aggregates(
        total_distance=total_distance,
        total_calories=total_calories,
        total_output_kj=perf.summary.total_output_kj if perf.summary else None,
        avg_power=avg_power,
        max_power=max_power,
        avg_heart_rate=avg_heart_rate,
        max_heart_rate=max_heart_rate,
        avg_cadence=avg_cadence,
        max_cadence=max_cadence,
        avg_speed=avg_speed,
        max_speed=max_speed,
    )


def _lap_message(
    mapping: SportMapping,
    workout: PelotonWorkout,
    perf: WorkoutPerformance | None,
    start_ms: int,
    end_ms: int,
    end_offset: int,
) -> LapMessage:
    agg = _aggregate(perf)
    lap = LapMessage()
    lap.event = Event.LAP
    lap.event_type = EventType.STOP
    lap.message_index = 0
    lap.sport = mapping.sport
    if mapping.sub_sport is not None:
        lap.sub_sport = mapping.sub_sport
    lap.start_time = start_ms
    lap.timestamp = end_ms
    lap.total_elapsed_time = float(end_offset)
    lap.total_timer_time = float(end_offset)
    if agg.total_distance is not None:
        lap.total_distance = agg.total_distance
    if agg.total_calories is not None:
        lap.total_calories = round(agg.total_calories)
    if agg.avg_power is not None:
        lap.avg_power = round(agg.avg_power)
    if agg.max_power is not None:
        lap.max_power = round(agg.max_power)
    if agg.avg_heart_rate is not None:
        lap.avg_heart_rate = round(agg.avg_heart_rate)
    if agg.max_heart_rate is not None:
        lap.max_heart_rate = round(agg.max_heart_rate)
    if agg.avg_cadence is not None:
        lap.avg_cadence = round(agg.avg_cadence)
    if agg.max_cadence is not None:
        lap.max_cadence = round(agg.max_cadence)
    if agg.avg_speed is not None:
        lap.avg_speed = agg.avg_speed
        lap.enhanced_avg_speed = agg.avg_speed
    if agg.max_speed is not None:
        lap.max_speed = agg.max_speed
        lap.enhanced_max_speed = agg.max_speed
    return lap


def _session_message(
    mapping: SportMapping,
    workout: PelotonWorkout,
    perf: WorkoutPerformance | None,
    start_ms: int,
    end_ms: int,
    end_offset: int,
) -> SessionMessage:
    agg = _aggregate(perf)
    session = SessionMessage()
    session.event = Event.LAP
    session.event_type = EventType.STOP
    session.message_index = 0
    session.sport = mapping.sport
    if mapping.sub_sport is not None:
        session.sub_sport = mapping.sub_sport
    session.sport_index = 1
    session.first_lap_index = 0
    session.num_laps = 1
    session.start_time = start_ms
    session.timestamp = end_ms
    session.total_elapsed_time = float(end_offset)
    session.total_timer_time = float(end_offset)
    if agg.total_distance is not None:
        session.total_distance = agg.total_distance
    if agg.total_calories is not None:
        session.total_calories = round(agg.total_calories)
    if agg.avg_power is not None:
        session.avg_power = round(agg.avg_power)
    if agg.max_power is not None:
        session.max_power = round(agg.max_power)
    if agg.avg_heart_rate is not None:
        session.avg_heart_rate = round(agg.avg_heart_rate)
    if agg.max_heart_rate is not None:
        session.max_heart_rate = round(agg.max_heart_rate)
    if agg.avg_cadence is not None:
        session.avg_cadence = round(agg.avg_cadence)
    if agg.max_cadence is not None:
        session.max_cadence = round(agg.max_cadence)
    if agg.avg_speed is not None:
        session.avg_speed = agg.avg_speed
        session.enhanced_avg_speed = agg.avg_speed
    if agg.max_speed is not None:
        session.max_speed = agg.max_speed
        session.enhanced_max_speed = agg.max_speed
    if workout.ftp is not None:
        session.threshold_power = round(workout.ftp)
    total_work_kj = agg.total_output_kj if agg.total_output_kj is not None else workout.total_work
    if total_work_kj is not None:
        session.total_work = total_work_kj
    session.sport_profile_name = _profile_name(mapping.label, workout)
    return session


def _profile_name(label: str, workout: PelotonWorkout) -> str:
    name = f"Peloton {label}: {workout.title}"
    if workout.instructor:
        name += f" with {workout.instructor}"
    return name[:MAX_PROFILE_NAME_CHARS]


#: Seconds between the Unix epoch (1970-01-01) and the FIT epoch (1989-12-31).
FIT_EPOCH_OFFSET_S = 631_065_600


def _fit_epoch_local_seconds(ts_ms: int) -> int:
    """FIT ``local_timestamp`` is uint32 seconds in the FIT epoch, adjusted
    westward by the local UTC offset (wall-clock time)."""
    offset = datetime.now(UTC).astimezone().utcoffset()
    offset_s = int(offset.total_seconds()) if offset else 0
    return (ts_ms // 1000 - offset_s - FIT_EPOCH_OFFSET_S) & 0xFFFFFFFF


def _add_strength_structure(
    builder: FitFileBuilder, plan: list[ExerciseBlock], start_ms: int
) -> None:
    """Emit per-exercise structure for strength sessions: one
    ``exercise_title`` + one ``set`` message per class-plan block.

    COROS derives muscle heatmaps by matching exercise names against its
    library, so the names come straight from the Peloton class plan.
    """
    timestamp_ms = start_ms
    for index, block in enumerate(plan):
        title = ExerciseTitleMessage()
        title.message_index = index
        # fit_tool types exercise_name as a uint16 table index; the string
        # field is workout_step_name - COROS/Garmin readers use the string.
        title.workout_step_name = block.name
        builder.add(title)

        work = SetMessage()
        work.message_index = index
        work.timestamp = timestamp_ms
        work.start_time = timestamp_ms
        work.duration = block.duration_s * 1000  # ms
        work.set_type = 1  # active
        builder.add(work)

        timestamp_ms += block.duration_s * 1000


def _activity_message(end_ms: int, end_offset: int) -> ActivityMessage:
    activity = ActivityMessage()
    activity.type = FileType.ACTIVITY
    activity.event = Event.ACTIVITY
    activity.event_type = EventType.STOP
    activity.num_sessions = 1
    activity.local_timestamp = _fit_epoch_local_seconds(end_ms)
    activity.timestamp = end_ms
    activity.total_timer_time = float(end_offset)
    return activity


def _self_check(data: bytes) -> None:
    """Parse the produced file and run wire-level validation."""
    try:
        fit = FitFile.from_bytes(data)
        report = fit.validate()
    except Exception as exc:
        raise FitBuildError(f"Built FIT file failed to parse: {exc}") from exc
    if getattr(report, "has_errors", False):
        raise FitBuildError(f"Built FIT file failed validation: {report.errors[:5]}")


def workout_start_utc(workout: PelotonWorkout) -> datetime:
    return datetime.fromtimestamp(workout.start_time, tz=UTC)
