"""Tests for pelocore.peloton (performance parsing + HTTP wrapper)."""

from __future__ import annotations

from typing import Any

import responses

from pelocore.peloton import (
    LocationPoint,
    PylotonClient,
    parse_performance,
)

# -- pure parsing tests -----------------------------------------------------


def test_parse_indoor_metrics() -> None:
    pg = {
        "duration": 3,
        "metrics": [
            {"slug": "output", "values": [100, None, 120]},
            {"slug": "cadence", "values": [85, 85, 86]},
            {"slug": "heart_rate", "values": [120, 122, 124]},
            {"slug": "speed", "values": [12.0, 12.5, 13.0]},
            {"slug": "distance", "values": [0.0, 0.01, 0.02]},
            {"slug": "calories", "values": [0, 1, 2]},
        ],
        "seconds_since_pedaling_start": [0, 2, 5],
    }
    perf = parse_performance(pg)
    assert perf.duration_s == 3
    assert perf.locations == []
    assert [s.offset for s in perf.samples] == [0, 2, 5]
    first = perf.samples[0]
    assert first.power == 100.0
    assert first.cadence == 85.0
    assert first.heart_rate == 120.0
    assert first.distance_m == 0.0
    # mph -> m/s and miles -> meters conversions
    speed = perf.samples[1].speed_ms
    assert speed is not None
    assert abs(speed - 12.5 * 0.44704) < 1e-9
    distance = perf.samples[2].distance_m
    assert distance is not None and abs(distance - 0.02 * 1609.344) < 1e-9
    # None metrics pass through as None
    assert perf.samples[1].power is None


def test_parse_indoor_missing_offsets_uses_index() -> None:
    pg = {"duration": 2, "metrics": [{"slug": "output", "values": [1, 2]}]}
    perf = parse_performance(pg)
    assert [s.offset for s in perf.samples] == [0, 1]


def test_parse_outdoor_locations() -> None:
    pg = {
        "duration": 2,
        "metrics": [{"slug": "heart_rate", "values": [150, 151]}],
        "location_data": [
            {
                "coordinates": [
                    {
                        "seconds_offset_from_start": 0,
                        "latitude": 45.5,
                        "longitude": -122.6,
                        "distance": 0.1,
                        "heart_rate": 150,
                        "speed": 10.0,
                    },
                    {
                        "seconds_offset_from_start": 1,
                        "latitude": 45.5,
                        "longitude": -122.6,
                        "distance": 0.2,
                        "heart_rate": 151,
                    },
                ]
            }
        ],
    }
    perf = parse_performance(pg)
    assert len(perf.locations) == 2
    assert isinstance(perf.locations[0], LocationPoint)
    distance = perf.locations[0].distance_m
    assert distance is not None
    assert abs(distance - 0.1 * 1609.344) < 1e-9
    assert perf.samples[0].heart_rate == 150.0
    speed = perf.samples[0].speed_ms
    assert speed is not None
    assert speed == 10.0 * 0.44704


def test_parse_malformed_location_skipped() -> None:
    perf = parse_performance(
        {"location_data": [{"coordinates": [{"seconds_offset_from_start": "x"}]}, None]}
    )
    assert perf.locations == []


# -- HTTP wrapper tests ------------------------------------------------------


def _mock_peloton_auth() -> None:
    responses.add(
        responses.GET,
        "https://api.onepeloton.com/api/me",
        json={"error": "unauthorized"},
        status=401,
    )
    responses.add(
        responses.POST,
        "https://auth.onepeloton.com/oauth/token",
        json={"access_token": "at", "id_token": "it", "refresh_token": "rt"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://api.onepeloton.com/api/me",
        json={"id": "uid1", "username": "ryder", "total_workouts": 3},
        status=200,
    )


FUTURE = 4_102_444_800  # 2100-01-01: always inside the backfill window
PAST = 1_700_000_000  # always outside the backfill window


def _workout(wid: str, start: int) -> dict[str, Any]:
    return {
        "id": wid,
        "status": "COMPLETE",
        "fitness_discipline": "cycling",
        "is_outdoor": False,
        "start_time": start,
        "end_time": start + 600,
        "name": wid,
        "ride": {"title": f"Ride {wid}", "instructor": {"name": "Denis"}},
    }


@responses.activate
def test_workouts_since_paginates_and_stops_at_cutoff() -> None:
    _mock_peloton_auth()
    for page in (0, 1, 2):
        responses.add(
            responses.GET,
            f"https://api.onepeloton.com/api/user/uid1/workouts?sort_by=-created&page={page}&limit=100",
            json={"data": [_workout(f"p{page}", FUTURE)] * (100 if page < 2 else 0)},
            status=200,
        )

    client = PylotonClient(username="ryder", password="pw")
    workouts = client.workouts_since(7)
    assert len(workouts) == 200  # both pages fully inside the window


@responses.activate
def test_workouts_since_stops_on_old_workout() -> None:
    _mock_peloton_auth()
    responses.add(
        responses.GET,
        "https://api.onepeloton.com/api/user/uid1/workouts?sort_by=-created&page=0&limit=100",
        json={"data": [_workout("new", FUTURE), _workout("old", PAST)]},
        status=200,
    )

    client = PylotonClient(username="ryder", password="pw")
    workouts = client.workouts_since(7)
    assert [w.id for w in workouts] == ["new"]


@responses.activate
def test_workout_normalization() -> None:
    _mock_peloton_auth()
    responses.add(
        responses.GET,
        "https://api.onepeloton.com/api/user/uid1/workouts?sort_by=-created&page=0&limit=100",
        json={"data": [_workout("w1", FUTURE)]},
        status=200,
    )
    client = PylotonClient(username="ryder", password="pw")
    workout = client.workouts_since(7)[0]
    assert workout.id == "w1"
    assert workout.status == "COMPLETE"
    assert workout.title == "Ride w1"
    assert workout.instructor == "Denis"


@responses.activate
def test_performance_fetch() -> None:
    _mock_peloton_auth()
    responses.add(
        responses.GET,
        "https://api.onepeloton.com/api/workout/w1/performance_graph?every_n=1",
        json={
            "duration": 2,
            "metrics": [{"slug": "output", "values": [50, 60]}],
            "seconds_since_pedaling_start": [0, 1],
        },
        status=200,
    )
    client = PylotonClient(username="ryder", password="pw")
    perf = client.performance("w1")
    assert perf.duration_s == 2
    assert perf.samples[1].power == 60.0
