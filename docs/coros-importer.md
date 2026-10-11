# COROS FIT importer reference

What COROS does with an activity file uploaded through the Training Hub
import pipeline, as of 2026-10-11. Established empirically:
[scripts/experiment_stretch_sport.py](../scripts/experiment_stretch_sport.py)
re-uploaded one real Peloton session with every candidate encoding (22
trials), plus byte-exact re-imports of native watch exports (yoga, pilates,
multisport). When COROS ships changes, re-run the harness and update this
doc.

## The model

The importer categorizes a file in two steps:

1. **Pair overrides**: `(primary sport, sub_sport)` looked up first. Only
   the `FITNESS_EQUIPMENT` family has entries:

   | pair | COROS sportType | label |
   |---|---|---|
   | `FITNESS_EQUIPMENT` + `PILATES` | 905 | Pilates |
   | `FITNESS_EQUIPMENT` + `ELLIPTICAL` | 903 | Elliptical |

   Unknown subsports in that family (`YOGA`, `FLEXIBILITY_TRAINING`) ERROR
   the import (errorSize=1, no activity) instead of falling back.

2. **Primary-sport table** (sub_sport ignored for everything else), with
   each family defaulting to its indoor variant:

   | primary sport | COROS sportType | label |
   |---|---|---|
   | `RUNNING` | 101 | Indoor Run |
   | `CYCLING` | 201 | Indoor Cycling |
   | `ROWING` | 701 | Indoor Rower |
   | `FITNESS_EQUIPMENT` | 903 | Elliptical |
   | `TRAINING` | 402 | Strength |
   | `GENERIC` | 400 | Gym Cardio |
   | `JUMP_ROPE` | 901 | Jump Rope |
   | `FLOOR_CLIMBING` | 902 | Stair Climb |
   | `BOXING` | 906 | Boxing |

3. **Unmapped primaries**: `WALKING`, `MEDITATION`, `DANCE`, `MOBILITY`,
   `HIIT`, `MULTISPORT`. `HIIT` and `MULTISPORT` imports fail outright
   (errorSize=1, no activity); `WALKING` once landed uncategorized
   (sportType 65535); the rest were dropped with no activity at all.

The full COROS taxonomy (from the Training Hub web app) also includes
codes pelocore never emits: 400 Cardio (Gym Cardio), 401 GPS Cardio,
900 Walk, 10001 Multi Sport, 1200 hyrox (displayed as "Hybrid Fitness"),
10000 Triathlon, plus ball sports and fishing families.

## Failure modes

| symptom | cause |
|---|---|
| import succeeds, no activity, errorSize=0 | file start time is in the future |
| import succeeds, errorSize=1 | broken CRC, unknown equipment subsport, `MULTISPORT`/`HIIT`/`DANCE`/`MOBILITY`/`MEDITATION` primary sport |
| import succeeds, activity lands uncategorized (65535) | `WALKING` (observed once) |

Two gotchas around this: the local state file records "uploaded" as soon as
the upload is registered, so state can claim an activity exists when the
importer actually dropped it; and a single-day query (`startDay == endDay`)
has returned empty results - always scope activity lookups across two days.

## Watch path vs import path

The watch sync and the FIT import are different categorization pipelines.
Native encodings, captured from real watch exports:

- **Yoga**: the watch writes `sport=TRAINING` + `sub_sport=YOGA` and syncs
  it as 904 Yoga. The same bytes through the importer land as 402 Strength
  (proven by re-importing the byte-exact watch export).
- **Pilates**: the watch writes `sport=FITNESS_EQUIPMENT` +
  `sub_sport=PILATES` and syncs it as 905 Pilates. The importer honors this
  pair (also proven by byte-exact re-import) - it is the one non-GPS
  discipline with a faithful import mapping.
- **Multisport**: watch exports are ZIPs of per-leg single-sport FITs (for
  example a cycling leg `CYCLING`+`INDOOR_CYCLING` and a strength leg
  `TRAINING`+`STRENGTH_TRAINING` with `set` messages). No "multisport"
  sport value appears in any file - the grouped 10001 Multisport activity
  is assembled by the watch sync path only. Re-importing the byte-exact
  ZIP creates separate per-leg activities (201 + 402), and a FIT carrying
  `sport=MULTISPORT` is rejected outright.

A direct S3 PUT of a zip (no inner `{md5}/{file}` wrapper) imports cleanly,
mirroring the web portal's bulk upload.

## Unreachable categories

- **Yoga (904)**: watch path only; the `TRAINING`+`YOGA` pair is absent
  from the importer's table. A TCX labeled `Sport="Yoga"` does not help -
  unknown sport strings convert to `GENERIC` and land as Gym Cardio (400).
  Filed with COROS support (2026-10-11). Unblocks if COROS adds the pair,
  an activity-type API, or app-side type editing (the app's edit UI is
  title-only today).
- **Hybrid Fitness (1200, "hyrox")**: exists in the taxonomy and web app,
  but not as a watch sport mode, so there is no encoding reference to
  learn from.
- **Grouped Multisport (10001)**: no import route. An untested variant that
  might behave differently: multiple sessions in ONE FIT file (COROS
  exports per-leg files in a ZIP instead).
- **Manufacturer spoofing** (file_id manufacturer 294 / product 822 plus a
  matching `device_info` message, cloned from a real COROS export) changes
  nothing: categorization is a server-side rule with no COROS-device
  special case.
- COROS's officially supported import modes (Run, Indoor Run, Trail Run,
  Track Run, Bike, Indoor Bike, Pool Swim, Open Water, Mountain Climb,
  Hike, Multisport, Bouldering, GPS Cardio, Strength, Indoor Climb, Gym
  Cardio, ball sports) are stale as documentation: rowing imports fine
  despite being unlisted, and the list understates what the pipeline
  accepts. But yoga/stretching really are unsupported.

## Post-import constraints

- **Fold-in-place**: re-uploading a FIT whose start time matches an
  existing activity updates it in place (same labelId) - the category is
  re-derived from the file's encoding and the display name resets to the
  sport default (a TCX fold is an observed exception: the name survived).
  No duplicate is ever created; rename after any re-upload. This is what
  makes targeted recategorization possible without delete+recreate churn
  (see `scripts/recategorize_bootcamps.py`).
- **Category is frozen after import**: `activity/update` renames only
  (type=1 with `name`; sportType-change payloads are rejected with result
  1031 or silently ignored), and the COROS app's edit UI exposes the title
  only. An imported activity's category cannot be changed afterwards.
- **FIT download endpoint**: `POST /activity/detail/download?labelId&
  sportType&fileType` (`accessToken` header; fileType 4=fit, 3=tcx, 2=kml,
  1=gpx, 0=csv) returns a JSON envelope with a presigned S3 `fileUrl`. It
  only serves device-recorded activities - imported ones return `data: {}`
  - and multisport downloads come back as ZIPs. COROS export CRCs match
  fit_tool's `crc16` over the data section.
- **Muscle heatmaps** are unavailable for imported activities: COROS
  derives them server-side from its own plan sessions, and native FIT
  exports contain no muscle data. **Training Load** for imported non-GPS
  workouts requires heart rate in the file; without it COROS reports load 0.

## How pelocore maps disciplines (converter v2)

Given the model above, `src/pelocore/sports.py` maps:

- `yoga` / `stretching` / `meditation` -> `GENERIC` -> Gym Cardio (400).
  Yoga is unreachable and Gym Cardio is the user-preferred reachable
  category over Strength.
- `pilates` -> `FITNESS_EQUIPMENT`+`PILATES` -> Pilates (905).
- every bootcamp variant (`bootcamp`, `caesar`, `circuit`,
  `bike_bootcamp`, `tread_bootcamp`, `row_bootcamp`) ->
  `TRAINING`+`STRENGTH_TRAINING` -> Strength (402).
- everything else to its sport family (`CYCLING`, `RUNNING`, `ROWING`,
  `WALKING`); cycling/running/walking drop the sub_sport outdoors.

Importer category hints (`coros_sport_code`) mirror this table with the
pair overrides first; `scripts/recategorize_bootcamps.py` shows the
fold-in-place pattern for re-categorizing history after a mapping change.
