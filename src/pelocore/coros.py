"""COROS Training Hub client implementing the unofficial upload protocol.

Flow (reverse-engineered from the Training Hub web app; see README):

1. ``POST /account/login`` with an MD5-hashed password (or a browser session
   token passed directly), yielding an ``accessToken``.
2. ``GET https://faq.coros.com/openapi/oss/sts`` returns base64-encoded,
   salt-prefixed temporary S3 credentials bound to a bucket per region.
3. The FIT file is wrapped in an uncompressed ZIP (``{md5}/{filename}``) and
   put to ``s3://{bucket}/fit_zip/{userId}/{md5}.zip`` with hand-rolled
   SigV4 authentication.
4. ``POST /activity/fit/import`` (multipart ``jsonParameter``) registers the
   object; ``POST /activity/fit/getImportSportList`` reports import progress
   (``status == 2`` means success).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import quote

import requests

REGION_BASE_URLS: dict[str, str] = {
    "en": "https://teamapi.coros.com",
    "eu": "https://teameuapi.coros.com",
    "cn": "https://teamcnapi.coros.com",
}

FAQ_API_URL = "https://faq.coros.com"
STS_APP_ID = "1660188068672619112"
STS_SALT = "9y78gpoERW4lBNYL"
STS_SIGN: dict[str, str] = {
    "en": "E34EF0E34A498A54A9C3EAEFC12B7CAF",
    "eu": "877571111A1EE5316E4B590103D4B5B3",
}
STS_BUCKET: dict[str, str] = {"en": "coros-s3", "eu": "eu-coros", "cn": "coros-oss"}
STS_SERVICE: dict[str, str] = {"en": "aws", "eu": "aws", "cn": "aliyun"}
#: Training Hub web BFF proxy. Since 2026-10-03 the open STS endpoint
#: (faq.coros.com/openapi/oss/sts) is offline and STS credentials are fetched
#: through this proxy, authenticated with the session token as a cookie.
STS_PROXY: dict[str, str] = {
    "en": "https://training.coros.com",
    "eu": "https://training.coros.com",
    "cn": "https://trainingcn.coros.com",
}

#: Login requests must look like the Training Hub web app or COROS may treat
#: them as a bot.
LOGIN_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/92.0.4515.39 Safari/537.36"
    ),
    "referer": "https://teamapi.coros.com/",
    "origin": "https://teamapi.coros.com/",
}

SUCCESS_RESULT = "0000"
IMPORT_STATUS_SUCCESS = 2

logger = logging.getLogger(__name__)


class CorosError(Exception):
    """Base class for COROS client failures."""


class CorosAuthError(CorosError):
    """Authentication failed or credentials are missing."""


class CorosHttpError(CorosError):
    """Non-JSON or otherwise malformed HTTP response."""


class CorosAmbiguousMatchError(CorosError):
    """More than one activity matched a start-time window; refusing to act."""


class CorosApiError(CorosError):
    """The API answered with a failure envelope."""

    def __init__(self, message: str, *, result: str, api_code: str | None = None):
        super().__init__(message)
        self.result = result
        self.api_code = api_code


@dataclass(frozen=True)
class S3Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    region: str
    bucket: str


@dataclass(frozen=True)
class CorosAccount:
    user_id: str
    nickname: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class ImportJob:
    id: str
    status: int
    original_filename: str | None
    error_size: int = 0


@dataclass(frozen=True)
class ActivityItem:
    label_id: str
    sport_type: int
    start_time: int  # epoch seconds
    name: str | None = None


@dataclass(frozen=True)
class UploadResult:
    import_id: str
    filename: str
    md5: str


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def password_md5(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()


def timezone_quarters(now: datetime | None = None) -> int:
    """COROS encodes timezone offsets in quarter-hours east of UTC."""
    dt = now if now is not None else datetime.now(UTC).astimezone()
    offset = dt.utcoffset()
    return int((offset.total_seconds() if offset else 0) // 900)


def build_upload_zip(content: bytes, md5_digest: str, filename: str) -> bytes:
    """Wrap ``content`` in the ZIP layout COROS expects: ``{md5}/{filename}``.

    Uses ``ZIP_STORED`` (no compression) and a fixed 1980 timestamp so output
    is deterministic for identical FIT bytes.
    """
    import io
    import zipfile

    buffer = io.BytesIO()
    date_time = (1980, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, data in ((f"{md5_digest}/", b""), (f"{md5_digest}/{filename}", content)):
            info = zipfile.ZipInfo(name, date_time=date_time)
            info.compress_type = zipfile.ZIP_STORED
            if name.endswith("/"):
                info.external_attr = 0x10 << 16
            zf.writestr(info, data)
    return buffer.getvalue()


def decode_sts_credentials(credentials_b64: str) -> S3Credentials:
    raw = base64.b64decode(credentials_b64.replace(STS_SALT, ""))
    data = json.loads(raw)
    return S3Credentials(
        access_key_id=data["AccessKeyId"],
        secret_access_key=data.get("SecretAccessKey") or data.get("AccessKeySecret", ""),
        session_token=data.get("SessionToken", ""),
        region=data["Region"],
        bucket=data["Bucket"],
    )


def _uri_encode(path: str) -> str:
    return "/".join(quote(segment, safe="") for segment in path.split("/"))


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sigv4_authorization(
    creds: S3Credentials,
    *,
    method: str,
    host: str,
    key: str,
    payload_hash: str,
    amz_date: str,
    content_type: str = "application/zip",
) -> str:
    """Build the SigV4 ``Authorization`` header for an S3 PUT (pure, testable)."""
    encoded_key = _uri_encode(key)
    token_header = f"x-amz-security-token:{creds.session_token}\n" if creds.session_token else ""
    signed_headers = (
        "content-type;host;x-amz-content-sha256;x-amz-date"
        + (";x-amz-security-token" if creds.session_token else "")
    )
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
        + token_header
    )
    canonical_request = "\n".join(
        [method, f"/{encoded_key}", "", canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{amz_date[:8]}/{creds.region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    k_date = _hmac(f"AWS4{creds.secret_access_key}".encode(), amz_date[:8])
    k_region = _hmac(k_date, creds.region)
    k_service = _hmac(k_region, "s3")
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={creds.access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


class CorosClient:
    #: Process-wide count of requests made to COROS hosts (API + STS + S3
    #: + import registration); exposed on the status /metrics endpoint so
    #: the API budget stays observable.
    api_calls: ClassVar[int] = 0

    def __init__(
        self,
        *,
        region: str = "en",
        email: str = "",
        password: str = "",
        access_token: str | None = None,
        timezone_quarters_override: int | None = None,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ):
        if region not in REGION_BASE_URLS:
            raise CorosError(f"Unknown COROS region: {region!r}")
        self._region = region
        self._email = email
        self._password = password
        self._timeout = timeout
        self._timezone_override = timezone_quarters_override
        self._http = session or requests.Session()
        self._token: str | None = access_token or None
        self._account: CorosAccount | None = None

    # -- authentication -----------------------------------------------------

    @property
    def base_url(self) -> str:
        return REGION_BASE_URLS[self._region]

    def ensure_auth(self) -> str:
        if self._token is None:
            self._login()
        assert self._token is not None
        return self._token

    def _login(self) -> None:
        if not self._email or not self._password:
            raise CorosAuthError(
                "No COROS credentials: set COROS_EMAIL/COROS_PASSWORD "
                "or provide COROS_ACCESS_TOKEN."
            )
        payload = {
            "account": self._email,
            "accountType": 2,
            "pwd": password_md5(self._password),
        }
        type(self).api_calls += 1
        try:
            response = self._http.post(
                self.base_url + "/account/login",
                data=json.dumps(payload),
                headers=LOGIN_HEADERS,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise CorosHttpError(f"COROS login failed: {exc}") from exc
        data = _parse_envelope(response, self.base_url + "/account/login")
        if not isinstance(data, dict):
            raise CorosAuthError("COROS login response missing data")
        if data.get("twoFactorRequired") or data.get("loginTicket"):
            raise CorosAuthError(
                "COROS account has two-factor authentication enabled; password "
                "login stops at the 2FA challenge. Use COROS_ACCESS_TOKEN from "
                "a browser session instead (see README), or disable 2FA."
            )
        token = data.get("accessToken")
        if not token:
            raise CorosAuthError("COROS login response missing accessToken")
        self._token = str(token)

    def account(self) -> CorosAccount:
        self.ensure_auth()
        data = self._api_get("account/query")
        account = CorosAccount(
            user_id=str(data.get("userId") or ""),
            nickname=data.get("nickname"),
            email=data.get("email"),
        )
        if not account.user_id:
            raise CorosApiError("COROS account response missing userId", result="0000")
        self._account = account
        return account

    # -- low-level request helpers ------------------------------------------

    def _api_get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        data = self._request("GET", self.base_url + "/" + path, params=params)
        return data if isinstance(data, dict) else {}

    def _api_post(self, path: str, body: Any, *, token_header: str = "accessToken") -> Any:
        return self._request(
            "POST",
            self.base_url + "/" + path,
            json_body=body,
            token_header=token_header,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        token_header: str = "accessToken",
        authenticate: bool = True,
        params: dict[str, str] | None = None,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if authenticate:
            headers[token_header] = self.ensure_auth()
        type(self).api_calls += 1
        try:
            response = self._http.request(
                method, url, headers=headers, json=json_body, params=params, timeout=self._timeout
            )
        except requests.RequestException as exc:
            raise CorosHttpError(f"COROS request failed: {exc}") from exc
        return _parse_envelope(response, url)

    # -- STS + S3 upload ----------------------------------------------------

    def _sts_credentials(self) -> S3Credentials:
        if self._region not in STS_SIGN:
            raise CorosError(
                f"Region {self._region!r} uses Aliyun storage; uploads are not supported."
            )
        errors: list[str] = []
        # Channel 1: Training Hub BFF proxy (2026-10-03+; requires the session
        # token as a cookie).
        self.ensure_auth()
        proxy_url = (
            f"{STS_PROXY[self._region]}/api/proxy/oss/sts"
            f"?bucket={STS_BUCKET[self._region]}&service={STS_SERVICE[self._region]}&v=2"
        )
        type(self).api_calls += 1
        try:
            response = self._http.get(
                proxy_url,
                headers={"Accept": "application/json", "Cookie": f"CPL-coros-token={self._token}"},
                timeout=self._timeout,
            )
            creds = _parse_sts_response(response, proxy_url)
            if creds is not None:
                return creds
            errors.append(f"web-proxy: HTTP {response.status_code} {response.text[:120]}")
        except (CorosHttpError, ValueError) as exc:
            errors.append(f"web-proxy: {exc}")
        except requests.RequestException as exc:
            errors.append(f"web-proxy: {exc}")

        # Channel 2: legacy open endpoint (offline since 2026-10-03; kept as a
        # fallback in case COROS restores it).
        params = {
            "bucket": STS_BUCKET[self._region],
            "service": STS_SERVICE[self._region],
            "v": "2",
            "app_id": STS_APP_ID,
            "sign": STS_SIGN[self._region],
        }
        legacy_url = FAQ_API_URL + "/openapi/oss/sts"
        type(self).api_calls += 1
        try:
            response = self._http.get(legacy_url, params=params, timeout=self._timeout)
            creds = _parse_sts_response(response, legacy_url)
            if creds is not None:
                return creds
            errors.append(f"legacy: HTTP {response.status_code} {response.text[:120]}")
        except (CorosHttpError, ValueError) as exc:
            errors.append(f"legacy: {exc}")
        except requests.RequestException as exc:
            errors.append(f"legacy: {exc}")

        raise CorosHttpError("STS failed on all channels: " + " | ".join(errors))

    def _s3_put(self, creds: S3Credentials, key: str, body: bytes) -> None:
        host = f"{creds.bucket}.s3.{creds.region}.amazonaws.com"
        url = f"https://{host}/{_uri_encode(key)}"
        amz_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {
            "Content-Type": "application/zip",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": sigv4_authorization(
                creds,
                method="PUT",
                host=host,
                key=key,
                payload_hash=payload_hash,
                amz_date=amz_date,
            ),
        }
        if creds.session_token:
            headers["x-amz-security-token"] = creds.session_token
        type(self).api_calls += 1
        try:
            response = self._http.put(url, headers=headers, data=body, timeout=self._timeout)
        except requests.RequestException as exc:
            raise CorosHttpError(f"S3 PUT failed: {exc}") from exc
        if response.status_code >= 400:
            text = response.text[:200]
            raise CorosHttpError(f"S3 PUT failed: HTTP {response.status_code} {text}")

    def _register_import(
        self,
        *,
        md5_digest: str,
        zip_size: int,
        object_key: str,
        bucket: str,
        service: str,
        filename: str,
    ) -> str:
        self.ensure_auth()
        body = {
            "source": 1,
            "timezone": self._timezone_quarters(),
            "bucket": bucket,
            "md5": md5_digest,
            "size": zip_size,
            "object": object_key,
            "serviceName": service,
            "oriFileName": filename,
        }
        url = self.base_url + "/activity/fit/import"
        type(self).api_calls += 1
        try:
            response = self._http.post(
                url,
                files={"jsonParameter": (None, json.dumps(body))},
                headers={"AccessToken": self._token or ""},
                timeout=max(self._timeout, 60.0),
            )
        except requests.RequestException as exc:
            raise CorosHttpError(f"COROS import registration failed: {exc}") from exc
        data = _parse_envelope(response, url)
        if not isinstance(data, dict):
            raise CorosApiError("COROS import response missing job id", result=SUCCESS_RESULT)
        import_id = data.get("id") or data.get("idString")
        if not import_id:
            raise CorosApiError("COROS import response missing job id", result=SUCCESS_RESULT)
        return str(import_id)

    def _timezone_quarters(self) -> int:
        if self._timezone_override is not None:
            return self._timezone_override
        return timezone_quarters()

    # -- public operations ---------------------------------------------------

    def upload_fit(self, fit_bytes: bytes, filename: str) -> UploadResult:
        digest = md5_hex(fit_bytes)
        zip_bytes = build_upload_zip(fit_bytes, digest, filename)
        creds = self._sts_credentials()
        account = self.account()
        object_key = f"fit_zip/{account.user_id}/{digest}.zip"
        self._s3_put(creds, object_key, zip_bytes)
        import_id = self._register_import(
            md5_digest=digest,
            zip_size=len(zip_bytes),
            object_key=object_key,
            bucket=creds.bucket,
            service=STS_SERVICE[self._region],
            filename=filename,
        )
        return UploadResult(import_id=import_id, filename=filename, md5=digest)

    def import_jobs(self, size: int = 50) -> list[ImportJob]:
        data = self._api_post("activity/fit/getImportSportList", {"size": size})
        if not isinstance(data, list):
            return []
        jobs: list[ImportJob] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            job_id = str(item.get("id") or item.get("idString") or "")
            if not job_id:
                continue
            jobs.append(
                ImportJob(
                    id=job_id,
                    status=int(item.get("status", -1)),
                    original_filename=item.get("originalFilename") or item.get("oriFileName"),
                    error_size=int(item.get("errorSize", 0) or 0),
                )
            )
        return jobs

    def imported_filenames(self) -> set[str]:
        return {job.original_filename for job in self.import_jobs() if job.original_filename}

    def imported_versions(self, size: int = 100) -> dict[str, int]:
        """workout_id -> highest version present in the import list.

        Filenames look like ``peloton-<id>.fit`` (version 1) or
        ``peloton-<id>.v<n>.fit`` (later converter versions). This is the
        stateless drift-detection source: version info arrives in the
        import-list read the sync already performs.
        """
        out: dict[str, int] = {}
        for job in self.import_jobs(size=size):
            name = job.original_filename or ""
            if not name.startswith("peloton-") or not name.endswith(".fit"):
                continue
            stem = name[len("peloton-") : -len(".fit")]
            workout_id, _, version = stem.partition(".v")
            version_num = int(version) if version.isdigit() else 1
            if workout_id and version_num > out.get(workout_id, 0):
                out[workout_id] = version_num
        return out

    def find_activity(
        self, start_time: int, sport_hint: int | None = None
    ) -> ActivityItem | None:
        """The unique activity within +/-60s of ``start_time``.

        Sport-hinted matches take precedence; a single unambiguous time-only
        match is accepted (the importer may file files under an unexpected
        sport). None when there is no match; ambiguity logs a warning and
        returns None - callers must never act on an ambiguous window.
        """
        hinted: dict[str, ActivityItem] = {}
        time_only: dict[str, ActivityItem] = {}
        window_day = datetime.fromtimestamp(start_time, tz=UTC)
        start_day = (window_day - timedelta(days=1)).strftime("%Y%m%d")
        end_day = (window_day + timedelta(days=1)).strftime("%Y%m%d")
        for page in (1, 2):
            try:
                activities = self.list_activities(
                    page=page, size=50, start_day=start_day, end_day=end_day
                )
            except CorosError as exc:
                logger.debug("activity query failed: %s", exc)
                continue
            for item in activities:
                if abs(item.start_time - start_time) > 60:
                    continue
                time_only[item.label_id] = item
                if sport_hint is not None and item.sport_type == sport_hint:
                    hinted[item.label_id] = item
        target_group = hinted or time_only
        if len(target_group) == 1:
            return next(iter(target_group.values()))
        if len(target_group) > 1:
            logger.warning(
                "ambiguous activity window for start_time=%s (%d candidates)",
                start_time,
                len(target_group),
            )
            raise CorosAmbiguousMatchError(
                f"{len(target_group)} activities within 60s of {start_time}"
            )
        return None

    def list_activities(
        self,
        *,
        page: int = 1,
        size: int = 50,
        start_day: str | None = None,
        end_day: str | None = None,
    ) -> list[ActivityItem]:
        """One page of activities (newest first). ``start_day``/``end_day``
        are YYYYMMDD strings.

        Always scope the query: an unbounded GET is both rejected in some
        shapes and, worse, observed to return stale partial results, which
        once made freshly imported activities invisible. Scoping is cheap
        and reliable.
        """
        params: dict[str, str] = {"pageNumber": str(page), "size": str(size)}
        if start_day is None or end_day is None:
            now = datetime.now(UTC)
            end_day = end_day or now.strftime("%Y%m%d")
            start_day = start_day or (now - timedelta(days=7)).strftime("%Y%m%d")
        params["startDay"] = start_day
        params["endDay"] = end_day
        data = self._request(
            "GET", self.base_url + "/activity/query", params=params
        )
        items: list[ActivityItem] = []
        if not isinstance(data, dict):
            return items
        for raw in data.get("dataList") or []:
            if not isinstance(raw, dict):
                continue
            label_id = raw.get("labelId")
            if not label_id:
                continue
            items.append(
                ActivityItem(
                    label_id=str(label_id),
                    sport_type=int(raw.get("sportType", 0) or 0),
                    start_time=int(raw.get("startTime", 0) or 0),
                    name=raw.get("name"),
                )
            )
        return items

    def all_activities(
        self, *, start_day: str | None = None, end_day: str | None = None
    ) -> list[ActivityItem]:
        """Every activity in range, following pagination."""
        out: list[ActivityItem] = []
        page = 1
        while True:
            batch = self.list_activities(
                page=page, start_day=start_day, end_day=end_day
            )
            out.extend(batch)
            if len(batch) < 50:
                return out
            page += 1

    def delete_activity(self, label_id: str) -> None:
        """Delete an activity by labelId (succeeds whether or not it exists)."""
        self._request(
            "GET",
            self.base_url + "/activity/delete",
            params={"labelId": label_id},
        )

    def rename_activity(self, label_id: str, name: str) -> None:
        """Rename an activity via ``activity/update``.

        The web app sends the token in an all-lowercase ``accesstoken``
        header plus a ``yfheader`` JSON blob (userId + language); mirror it.
        """
        self.ensure_auth()
        account = self.account()
        headers = {
            "Content-Type": "application/json",
            "accesstoken": self._token or "",
            "yfheader": json.dumps({"userId": account.user_id, "language": "en-US"}),
        }
        type(self).api_calls += 1
        try:
            response = self._http.post(
                self.base_url + "/activity/update",
                data=json.dumps({"type": 1, "labelId": label_id, "name": name}),
                headers=headers,
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise CorosHttpError(f"COROS rename failed: {exc}") from exc
        _parse_envelope(response, self.base_url + "/activity/update")

    def rename_after_import(
        self,
        start_time: int,
        sport_hint: int | None,
        name: str,
        *,
        timeout_s: float = 45.0,
        interval_s: float = 10.0,
    ) -> bool:
        """Resolve the labelId for a just-imported activity and rename it.

        COROS assigns the labelId while it processes the import, so poll
        ``activity/query`` (via :meth:`find_activity`, which requires a
        unique match) until found or the deadline passes. Returns True when
        renamed.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                item = self.find_activity(start_time, sport_hint)
            except CorosAmbiguousMatchError:
                return False  # ambiguous: never guess
            if item is not None:
                self.rename_activity(item.label_id, name)
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval_s)

    def remove_from_import_list(self, import_id: str) -> None:
        """Remove an entry from the import list (post-delete cleanup)."""
        self._api_post("activity/fit/deleteSportImport", {"importId": import_id})

    def wait_for_import(
        self, import_id: str, *, timeout_s: float, interval_s: float = 5.0
    ) -> ImportJob | None:
        """Poll until the import succeeds (status 2) or errors/timeouts.

        Returns the final known job, or ``None`` if the job never appeared.
        A timeout is *not* fatal: the upload itself already succeeded.
        """
        deadline = time.monotonic() + timeout_s
        last: ImportJob | None = None
        while True:
            for job in self.import_jobs():
                if job.id == import_id:
                    last = job
                    break
            if last is not None:
                if last.status == IMPORT_STATUS_SUCCESS:
                    return last
                if last.error_size > 0:
                    return last
            if time.monotonic() >= deadline:
                return last
            time.sleep(interval_s)


def _parse_sts_response(response: requests.Response, url: str) -> S3Credentials | None:
    """Parse an STS payload; ``None`` means "try the next channel".

    ``faq.coros.com`` uses ``{code, msg, data}`` (plain HTTP status); the
    Training Hub BFF proxy answers ``HTTP 200`` with ``{code: 200, ...}``.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise CorosHttpError(
            f"STS returned non-JSON (HTTP {response.status_code}) from {url}"
        ) from exc
    if response.status_code >= 400 or body.get("code") != 200:
        return None
    credentials = (body.get("data") or {}).get("credentials")
    if not credentials:
        return None
    try:
        return decode_sts_credentials(credentials)
    except (ValueError, KeyError) as exc:
        raise CorosHttpError("STS credentials could not be decoded") from exc


def _parse_envelope(response: requests.Response, url: str) -> Any:
    try:
        body = response.json()
    except ValueError as exc:
        raise CorosHttpError(
            f"COROS returned non-JSON (HTTP {response.status_code}) from {url}"
        ) from exc
    result = body.get("result") if isinstance(body, dict) else None
    if result != SUCCESS_RESULT:
        if isinstance(body, dict):
            message = body.get("message", "request failed")
        else:
            message = str(body)[:200]
        api_code = body.get("apiCode") if isinstance(body, dict) else None
        raise CorosApiError(f"COROS API error: {message}", result=str(result), api_code=api_code)
    if response.status_code >= 400:
        raise CorosHttpError(f"COROS HTTP {response.status_code} with success envelope")
    return body.get("data")
