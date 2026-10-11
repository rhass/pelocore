# pelocore

<p align="center">
  <picture>
        <source media="(prefers-color-scheme: dark)" srcset="assets/logo/logo-horizontal-reverse.svg">
        <source media="(prefers-color-scheme: light)" srcset="assets/logo/logo-horizontal.svg">
        <img src="assets/logo/logo-horizontal.svg" alt="pelocore" width="480">
  </picture>
</p>

Peloton → COROS activity bridge. Fetches your Peloton workouts, converts them
to Garmin FIT files, and uploads them to your COROS Training Hub - on a
schedule, with a built-in status page.

```
Peloton API ──► FIT builder ──► zip+md5 ──► COROS upload ──► state
(pylotoncycle)   (fit_tool)                (unofficial API)
```

## Features

- **Backfill sync**: picks up any workout from the last N days (default 7)
  that is not already in COROS - survives downtime and restarts.
- **All disciplines**: cycling, treadmill/outdoor running, rowing, walking,
  strength, yoga, stretching, meditation, pilates, bootcamps, cardio.
  Per-second metrics (power, cadence, heart rate, speed, distance) where
  Peloton provides them; summary-level sessions otherwise, with strength
  class plans exported as exercise structures.
- **Dedupe, two ways**: a local state file *and* reconciliation against the
  COROS import list (so a lost state file never double-uploads).
- **Status page**: HTML at `/`, JSON at `/api/status`, Prometheus metrics at
  `/metrics`, manual trigger via `POST /sync`. Optional bearer-token gate.
- **One-shot or service**: `pelocore sync` for k8s CronJobs, `pelocore run`
  for a long-running Deployment with the status server.

### Limitations

What COROS does with an imported activity is decided by its importer, based
on the FIT encoding alone. I mapped that behavior exhaustively by
re-uploading one real session with every candidate encoding
([scripts/experiment_stretch_sport.py](scripts/experiment_stretch_sport.py));
the full importer model lives in [AGENTS.md](AGENTS.md). Pilates is the
lone bright spot: it imports as Pilates (905), the only non-GPS discipline
that maps faithfully. Everything else has a catch:

- **yoga, stretching and meditation import as Gym Cardio (400)**. COROS's
  import pipeline cannot produce its Yoga category (904) - that exists only
  on the watch path, and neither the COROS app nor the API can change an
  activity's type after import. `GENERIC` is the closest reachable category.
- **bootcamps import as Strength (402)**. Hybrid Fitness (1200) and grouped
  Multisport (10001) are watch-path only; FITs encoding them are rejected.
- **walking imports land uncategorized** in COROS.
- **Muscle heatmaps are unavailable** for imported activities (COROS derives
  them server-side from its own plan sessions).
- **Training Load for imported non-GPS workouts needs heart rate**: without
  HR data COROS reports load 0. See Heart rate setup below.
- **Activity names**: the importer ignores FIT-provided names, so pelocore
  renames every activity to its Peloton title right after import.

## Quickstart (stateless)

Requires [mise](https://mise.jdx.dev) (Python 3.13, uv, ruff come from
`mise.toml`).

```console
$ mise run install                # uv sync --all-groups
$ export PELOTON_USERNAME=you@example.com
$ export PELOTON_PASSWORD='...'
$ export COROS_EMAIL=you@example.com
$ export COROS_PASSWORD='...'
$ mise exec -- pelocore doctor    # validates both sides
$ mise exec -- pelocore sync      # one-shot sync; safe to run on a schedule
```

`pelocore sync` is designed to need no state: dedupe runs against the COROS
import list (server-side, retained for many months) and the import pipeline
itself is md5-keyed, so even a re-upload of identical bytes cannot create a
duplicate. Anything that can run a container on a schedule is a complete
deployment. The shipped k8s CronJob mounts no volume:

```console
$ kubectl create namespace pelocore
$ cp deploy/k8s/secret.example.yaml secret.yaml   # fill in, then:
$ kubectl -n pelocore apply -f secret.yaml
$ kubectl -n pelocore apply -f deploy/k8s/cronjob.yaml    # every 15 min
```

The published image runs anywhere you can schedule a container
([Container image](#container-image)). `pelocore run` (long-running, with
the status page) is the alternative: `mise run start` locally, or
`deployment.yaml` in k8s.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PELOTON_USERNAME` / `PELOTON_PASSWORD` | - | Peloton credentials |
| `PELOTON_REFRESH_TOKEN` | - | Alternative to password login |
| `COROS_EMAIL` / `COROS_PASSWORD` | - | COROS credentials |
| `COROS_ACCESS_TOKEN` | - | Browser session token instead of password (value of the `CPL-coros-token` cookie on training.coros.com) |
| `COROS_REGION` | `en` | `en`, `eu`, or `cn` (uploads unsupported on `cn`) |
| `PELOCORE_TIMEZONE_QUARTERS` | host offset | COROS timezone in quarter-hours east of UTC (32 = UTC+8) |
| `PELOCORE_BACKFILL_DAYS` | `7` | How far back to look for unsynced workouts |
| `PELOCORE_SPORT_REMAPS` | none built-in | Optional discipline remaps, e.g. `meditation=yoga`; the default categories are listed under Limitations |
| `PELOCORE_AUTO_UPGRADE` | `on` | When a conversion change would produce different FIT bytes, delete the stale COROS activity and re-upload automatically. Conservative: ambiguous matches are skipped, never deleted blind. Set `off` to keep converter upgrades from re-importing history |
| `PELOCORE_IMPORT_POLL_SECONDS` | `60` | How long to poll COROS import status per upload |
| `PELOCORE_STATE_PATH` | `data/state.json` | State file location |
| `PELOCORE_SERVER_HOST` / `PELOCORE_SERVER_PORT` | `0.0.0.0` / `8080` | Status server bind |
| `PELOCORE_STATUS_TOKEN` | - | When set, `/`, `/api/status`, `/metrics`, `POST /sync` require `Authorization: Bearer <token>`; probes stay open |
| `PELOCORE_LOG_LEVEL` | `INFO` | Log verbosity |

Credentials may also live in a `.env` file (pydantic-settings loads it).

## CLI

```
pelocore sync     # run a single sync cycle and exit (cron mode)
pelocore run      # loop mode: internal scheduler + status server
pelocore status   # print cycle history from the state file
pelocore doctor   # verify Peloton and COROS credentials
```

Exit code of `pelocore sync` is `1` when the cycle outcome is `failed`
(partial failures still exit `0`; inspect via `pelocore status` or the logs).

## How COROS upload works

pelocore implements the reverse-engineered Training Hub upload protocol:

1. `POST /account/login` with an MD5-hashed password (or paste a browser
   token via `COROS_ACCESS_TOKEN`) → `accessToken`.
2. STS credentials for a per-region S3 bucket (Training Hub web proxy,
   legacy open endpoint as fallback).
3. The FIT file is zipped (`{md5}/peloton-{workoutId}.fit`, stored) and PUT
   to `s3://{bucket}/fit_zip/{userId}/{md5}.zip` with SigV4.
4. `POST /activity/fit/import` registers the object; import progress is
   polled via `activity/fit/getImportSportList` (`status == 2` = success).

If COROS rotates the STS signs, uploads fail with `401 signature error`
until the constants in `src/pelocore/coros.py` are updated.

## Backfilling history

```console
$ pelocore sync --since 2026-02-01 --chunk-size 10 --chunk-delay 30
```

- `--since YYYY-MM-DD` overrides the 7-day window (365-day cap); Peloton's
  history pagination supports up to 1000 workouts.
- `--chunk-size` / `--chunk-delay` pace the upload: after every N uploads the
  bridge pauses S seconds, keeping the request rate gentle on both APIs.
  Workouts upload oldest-first, so an interrupted run leaves the most recent
  ones for last.
- Backfills are resumable: state + the COROS import list dedupe every
  workout, so re-running skips what already landed (and even a full state
  loss cannot create duplicates, since the import pipeline is md5-keyed).
- `--dry-run` prints the plan (new/skip per workout) without uploading or
  re-fetching performance data.
- A 280-workout backfill (~8 months) takes roughly 45-70 minutes including
  chunk delays; per-workout failures are logged and retried on the next run.

## Stateless operation notes

- **Why it is safe**: the import pipeline is keyed by FIT content md5 -
  identical bytes yield the same import job and never a second activity -
  and the import list retains entries for many months. A lost state file
  costs wasted uploads, never duplicates.
- **The schedule is the knob**: a CronJob run costs ~3 light reads when
  there is nothing to do. The shipped default is `*/15`; relax it to lower
  API traffic.
- **Serverless** (Cloud Run Jobs, etc.): works the same way; tune
  `PELOCORE_IMPORT_POLL_SECONDS` down if per-invocation time is capped,
  since polling (import + rename resolution) dominates runtime. AWS
  Lambda's 15-minute cap fits small backfills only. `pelocore sync` is
  one-shot and ignores `PELOCORE_SYNC_INTERVAL_SECONDS` entirely - that
  setting only applies to `pelocore run`.
- **State file is optional**: it adds cycle history for the status page and
  skips already-known workouts (fewer API calls), but is never required for
  correctness.
- **API observability**: `/metrics` reports
  `pelocore_peloton_api_calls_total` and `pelocore_coros_api_calls_total`
  so the request budget stays visible.
- **Converter upgrades**: FIT filenames carry the converter version
  (`peloton-<id>.fit` = v1, `peloton-<id>.v2.fit` = v2). When the converter
  changes, sync can detect stale versions and upgrade, deleting the old
  activity and re-uploading. Set `PELOCORE_AUTO_UPGRADE=off` if you do not
  want history re-imported - with it off, only workouts imported after the
  upgrade get the new behavior.

## Heart rate setup: broadcast from your COROS wearable

COROS computes Training Load in the context of your other activities, but
Peloton workouts without heart rate data give it nothing to work with: for
imported strength, yoga and stretching sessions it reports load 0. pelocore
bridges that gap, but only if your Peloton workouts capture heart rate in
the first place.

Peloton hardware and the Peloton app cannot read a wrist-based wearable
directly. The way to get your HR into Peloton workouts is to **broadcast
heart rate from the COROS watch** (Settings → Broadcast Heart Rate on
supported models) and pair that broadcast as a standard Bluetooth heart-rate
sensor with whatever is running the class:

- **Peloton Bike / Tread / Row hardware**: add the broadcast as a Bluetooth
  HR sensor in the device's sensor-pairing screen.
- **Peloton Guide**: pair the broadcast as a Bluetooth HR sensor in its
  settings.
- **Peloton app (tablet or phone)**: pair the broadcast as a Bluetooth HR
  sensor from the app's heart-rate settings before starting the class.

Once paired, HR is captured by Peloton per workout and flows through
pelocore into the generated FIT files. That is what COROS needs to compute
Training Load for strength, yoga and stretching imports (and gives
cycling/running their full HR metrics).

## Kubernetes

Both modes are shipped under `deploy/k8s/`:

- **`cronjob.yaml`** - one-shot `pelocore sync` every 15 minutes. No volume
  needed: dedupe runs against the COROS import list. Visibility: logs.
- **`deployment.yaml`** - long-running `pelocore run` with the status page
  (ClusterIP Service, liveness/readiness probes, optional PVC for state).
  Keep it at 1 replica: syncers would race on the state file.

## Container image

Multi-arch (`linux/amd64`, `linux/arm64`), non-root (uid 65532), released to
GHCR on tags:

```console
ghcr.io/rhass/pelocore:vX.Y.Z
```

Pull and run:

```console
$ docker run -d -p 8080:8080 -v pelocore-data:/data \
    -e PELOTON_USERNAME=... -e PELOTON_PASSWORD=... \
    -e COROS_EMAIL=... -e COROS_PASSWORD=... \
    ghcr.io/rhass/pelocore:vX.Y.Z
```

Images are signed with Sigstore cosign (keyless) and carry SBOM +
provenance attestations:

```console
$ cosign verify \
    --certificate-identity-regexp \
      '^https://github\.com/rhass/pelocore/\.github/workflows/release\.yml@refs/tags/v.*' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    ghcr.io/rhass/pelocore:vX.Y.Z

$ cosign verify-attestation --type slsaprovenance \
    --certificate-identity-regexp \
      '^https://github\.com/rhass/pelocore/\.github/workflows/release\.yml@refs/tags/v.*' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    ghcr.io/rhass/pelocore:vX.Y.Z
```

## Development

```console
$ mise run install     # deps
$ mise run test        # pytest (unit + mocked integration)
$ mise run lint        # ruff
$ mise run typecheck   # mypy (strict)
```

Live smoke tests against the real services are opt-in:

```console
$ PELOCORE_LIVE=1 mise exec -- uv run pytest -m live
```

Release and CI internals (tag signing, local act runs, Chainguard base
image, the release version bump) are documented in
[AGENTS.md](AGENTS.md).

### Secrets via 1Password

Local credentials live in `.mise.local.toml` (git-ignored; see
[.mise.local.toml.example](.mise.local.toml.example)). The recommended
pattern uses 1Password secret references instead of plaintext values:

1. Add the 1Password CLI to the toolchain and sign in:

   ```console
   $ mise use 1password      # adds [tools] 1password = "latest" (to your local toml)
   $ op signin               # or unlock via the 1Password desktop app
   ```

2. Point env values at vault items - `op://<vault>/<item>/<field>`:

   ```toml
   [env]
   PELOTON_PASSWORD = "op://Private/pelocore/peloton-password"
   COROS_PASSWORD = "op://Private/pelocore/coros-password"
   ```

3. Wrap commands with `op run --`, which resolves every `op://` reference in
   the environment for the child process. The template ships task overrides
   for this (`mise start`, `mise run-sync`, `mise run-doctor`):

   ```toml
   [tasks.start]
   run = "op run -- uv run pelocore run"
   ```

Secrets resolved by `op run --` exist only in the child process environment -
references never touch disk or shell history. Plain values also work in
`.mise.local.toml` but are stored in plaintext; prefer references.

## Security notes

- Trivy scans run on every PR (filesystem: dependency vulns via `uv.lock` +
  secrets; config: Dockerfile/k8s misconfigurations) and on every release
  (image gate, HIGH/CRITICAL, unfixed ignored).
- Keyless cosign signatures publish image digests to the public Rekor
  transparency log. Fine for a public repo; be aware if you fork privately.
- The status page never renders credentials, only workout metadata and
  errors. Protect it with `PELOCORE_STATUS_TOKEN` when exposing beyond the
  cluster.

## Disclaimer

This project is not affiliated with, endorsed by, or approved by Peloton
Interactive, Inc. or COROS Wearables, Inc. It depends on **unofficial,
reverse-engineered APIs** that may change or stop working at any time, and
its use may violate the respective Terms of Service. Use at your own risk,
with your own credentials, for personal data you own.

## License

Apache-2.0 - see [LICENSE](LICENSE).
