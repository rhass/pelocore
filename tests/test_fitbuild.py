"""Tests for pelocore.fitbuild: build FIT files, reparse, validate."""

from __future__ import annotations

from typing import Any

from fit_tool import FitFile
from fit_tool.profile.profile_type import Sport, SubSport

from pelocore.fitbuild import build_activity_fit
from pelocore.peloton import (
    ExerciseBlock,
    LocationPoint,
    PerformanceSample,
    PerformanceSummary,
    WorkoutPerformance,
)
from tests.conftest import make_workout

START = 1_700_000_000


def row_fields(row: list[Any]) -> dict[str, Any]:
    """``to_rows`` data row layout: [Type, LocalID, Message, field, value, units, ...]."""
    fields = {}
    i = 3
    while i < len(row):
        fields[row[i]] = row[i + 1] if i + 1 < len(row) else None
        i += 3
    return fields


def data_rows(data: bytes, message: str) -> list[dict[str, Any]]:
    fit = FitFile.from_bytes(data)
    return [
        row_fields(r)
        for r in fit.to_rows()
        if r[0] == "Data" and r[2] == message
    ]


def test_cycling_build_validates_and_round_trips() -> None:
    workout = make_workout()
    perf = WorkoutPerformance(
        duration_s=10,
        samples=[
            PerformanceSample(
                offset=i,
                power=100 + i,
                cadence=85,
                heart_rate=120 + i,
                distance_m=i * 10.0,
                calories=i * 0.5,
            )
            for i in range(10)
        ],
        locations=[],
    )
    result = build_activity_fit(workout, perf)
    assert result.record_count == 10
    assert result.sport == "CYCLING" and result.sub_sport == "INDOOR_CYCLING"

    fit = FitFile.from_bytes(result.data)
    report = fit.validate()
    assert not report.has_errors, report.errors

    rows = data_rows(result.data, "record")
    assert len(rows) == 10
    assert rows[0]["power"] == 100
    assert rows[0]["cadence"] == 85
    assert rows[0]["heart_rate"] == 120

    sessions = data_rows(result.data, "session")
    assert len(sessions) == 1
    session = sessions[0]
    assert session["sport"] == Sport.CYCLING.value
    assert session["sub_sport"] == SubSport.INDOOR_CYCLING.value
    assert session["event"] == 9  # LAP
    assert session["event_type"] == 1  # STOP
    assert session["threshold_power"] == 250
    assert session["total_work"] == 586.0
    assert "Power Zone Ride" in session["sport_profile_name"]
    assert session["total_distance"] == 90.0

    laps = data_rows(result.data, "lap")
    assert laps[0]["avg_power"] == round(sum(100 + i for i in range(10)) / 10)
    assert laps[0]["max_power"] == 109

    activity = data_rows(result.data, "activity")
    assert activity[0]["num_sessions"] == 1

    # file_id sanity
    file_id = data_rows(result.data, "file_id")[0]
    assert file_id["type"] == 4  # FileType.ACTIVITY
    assert file_id["manufacturer"] == 255  # DEVELOPMENT


def test_treadmill_speed_conversion() -> None:
    workout = make_workout(discipline="running", title="Tread")
    mph = 6.0
    perf = WorkoutPerformance(
        duration_s=2,
        samples=[
            PerformanceSample(offset=0, speed_ms=mph * 0.44704, distance_m=0.0),
            PerformanceSample(offset=1, speed_ms=mph * 0.44704, distance_m=2.68224),
        ],
        locations=[],
    )
    result = build_activity_fit(workout, perf)
    fit = FitFile.from_bytes(result.data)
    assert not fit.validate().has_errors
    sessions = data_rows(result.data, "session")
    assert sessions[0]["sport"] == Sport.RUNNING.value
    assert sessions[0]["sub_sport"] == SubSport.TREADMILL.value
    assert abs(data_rows(result.data, "record")[1]["distance"] - 2.68224) < 0.01


def test_outdoor_run_includes_positions() -> None:
    workout = make_workout(discipline="running", outdoor=True, title="Outrun")
    perf = WorkoutPerformance(
        duration_s=2,
        samples=[],
        locations=[
            LocationPoint(offset=0, lat=45.5123, lon=-122.6543, distance_m=0.0, heart_rate=150.0),
            LocationPoint(
                offset=1, lat=45.5124, lon=-122.6544, distance_m=5.0, heart_rate=151.0, speed_ms=3.0
            ),
        ],
    )
    result = build_activity_fit(workout, perf)
    assert result.record_count == 2
    fit = FitFile.from_bytes(result.data)
    assert not fit.validate().has_errors
    rows = data_rows(result.data, "record")
    assert abs(rows[0]["position_lat"] - 45.5123) < 1e-5
    assert abs(rows[1]["position_long"] - (-122.6544)) < 1e-5
    assert rows[1]["heart_rate"] == 151


def test_summary_only_build_for_strength() -> None:
    workout = make_workout(discipline="strength", title="Strength 30")
    result = build_activity_fit(workout, None)
    # a single timestamp-only anchor record keeps the file FILE_TYPE-conformant
    assert result.record_count == 1
    fit = FitFile.from_bytes(result.data)
    assert not fit.validate().has_errors
    sessions = data_rows(result.data, "session")
    assert sessions[0]["sport"] == Sport.TRAINING.value
    assert sessions[0]["sub_sport"] == SubSport.STRENGTH_TRAINING.value
    assert sessions[0]["total_timer_time"] == 900  # end_time - start_time


def test_end_offset_uses_duration_when_present() -> None:
    workout = make_workout()  # end_time - start_time = 900
    perf = WorkoutPerformance(
        duration_s=30, samples=[PerformanceSample(offset=29, power=50)], locations=[]
    )
    result = build_activity_fit(workout, perf)
    session = data_rows(result.data, "session")[0]
    assert session["total_timer_time"] == 30


def test_cycling_session_carries_distance_and_speed() -> None:
    """Cycling: distance comes from the summaries block (Peloton has no
    per-second distance series), speed from the speed slug; both classic and
    enhanced session fields are written."""
    workout = make_workout()
    perf = WorkoutPerformance(
        duration_s=10,
        summary=PerformanceSummary(
            total_distance_m=2298.7 * 1609.344 / 1000, total_calories=100.0, total_output_kj=41.0
        ),
        samples=[
            PerformanceSample(offset=i, power=100 + i, cadence=85, heart_rate=120 + i,
                              speed_ms=4.8 + i * 0.1)
            for i in range(10)
        ],
        locations=[],
    )
    result = build_activity_fit(workout, perf)
    fit = FitFile.from_bytes(result.data)
    assert not fit.validate().has_errors, fit.validate().errors

    session = data_rows(result.data, "session")[0]
    assert abs(session["total_distance"] - 2298.7 * 1609.344 / 1000) < 0.1
    assert abs(session["max_speed"] - (4.8 + 0.9)) < 0.01
    assert abs(session["enhanced_max_speed"] - (4.8 + 0.9)) < 0.01
    assert session["avg_speed"] > 0
    assert abs(session["total_work"] - 41.0) < 0.01
    assert session["total_calories"] == 100

    records = data_rows(result.data, "record")
    # cumulative distance integrated from speed: ~sum(speed_ms * 1s)
    assert records[-1]["distance"] > 0


def test_running_distance_slug_wins_over_integration() -> None:
    workout = make_workout(discipline="running")
    perf = WorkoutPerformance(
        duration_s=3,
        summary=PerformanceSummary(total_distance_m=999.0),
        samples=[
            PerformanceSample(offset=i, speed_ms=3.0, distance_m=i * 10.0)
            for i in range(3)
        ],
        locations=[],
    )
    result = build_activity_fit(workout, perf)
    session = data_rows(result.data, "session")[0]
    assert session["total_distance"] == 999.0  # Peloton summary is authoritative


def test_strength_sets_and_exercise_titles() -> None:
    workout = make_workout(discipline="strength", title="Metal Full Body Strength")
    plan = [
        ExerciseBlock(name="Squat Jumps", duration_s=45, muscle_groups=("quads", "glutes")),
        ExerciseBlock(name="Push-ups", duration_s=45, muscle_groups=("chest",)),
    ]
    result = build_activity_fit(workout, None, plan=plan)
    fit = FitFile.from_bytes(result.data)
    assert not fit.validate().has_errors, fit.validate().errors

    rows = fit.to_rows()
    titles = [r for r in rows if r[0] == "Data" and r[2] == "exercise_title"]
    sets = [r for r in rows if r[0] == "Data" and r[2] == "set"]
    assert len(titles) == 2
    assert len(sets) == 2

    def fields(row: list[Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        i = 3
        while i < len(row):
            out[str(row[i])] = row[i + 1] if i + 1 < len(row) else None
            i += 3
        return out

    assert "Squat Jumps" in str(fields(titles[0]).values())
    set_fields = fields(sets[0])
    assert set_fields.get("duration") == 45000  # ms
    assert set_fields.get("set_type") == 1  # active
