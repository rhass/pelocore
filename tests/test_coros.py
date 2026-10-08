"""Tests for pelocore.coros: protocol helpers, SigV4, and the upload flow."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import responses

from pelocore.coros import (
    STS_SALT,
    CorosApiError,
    CorosAuthError,
    CorosClient,
    CorosHttpError,
    S3Credentials,
    build_upload_zip,
    decode_sts_credentials,
    password_md5,
    sigv4_authorization,
    timezone_quarters,
)

# -- pure helpers ------------------------------------------------------------


def test_password_md5() -> None:
    assert password_md5("abc") == hashlib.md5(b"abc").hexdigest()


def test_timezone_quarters_explicit_tz() -> None:
    dt = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    assert timezone_quarters(dt) == 32
    dt_minus = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert timezone_quarters(dt_minus) == -28


def test_decode_sts_credentials() -> None:
    creds_json = {
        "AccessKeyId": "AKID",
        "SecretAccessKey": "SK",
        "SessionToken": "TOK",
        "Region": "us-east-1",
        "Bucket": "coros-s3",
    }
    salted = STS_SALT + base64.b64encode(json.dumps(creds_json).encode()).decode()
    creds = decode_sts_credentials(salted)
    assert creds == S3Credentials(
        access_key_id="AKID",
        secret_access_key="SK",
        session_token="TOK",
        region="us-east-1",
        bucket="coros-s3",
    )


def test_build_upload_zip_layout() -> None:
    data = build_upload_zip(b"FITDATA", "abc123", "peloton-w1.fit")
    zf = zipfile.ZipFile(io.BytesIO(data))
    infos = zf.infolist()
    assert [i.filename for i in infos] == ["abc123/", "abc123/peloton-w1.fit"]
    assert all(i.compress_type == zipfile.ZIP_STORED for i in infos)
    assert zf.read("abc123/peloton-w1.fit") == b"FITDATA"
    assert zf.read("abc123/") == b""
    # deterministic
    assert data == build_upload_zip(b"FITDATA", "abc123", "peloton-w1.fit")


def test_build_upload_zip_sets_utf8_flag_for_unicode_names() -> None:
    data = build_upload_zip(b"D", "m", "peléoton-w1.fit")
    info = zipfile.ZipFile(io.BytesIO(data)).infolist()[-1]
    assert info.flag_bits & 0x800


# -- SigV4 (golden vector computed independently with openssl) ---------------


def test_sigv4_golden_vector() -> None:
    creds = S3Credentials(
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRiCYEXAMPLEKEY",
        session_token="",
        region="us-west-2",
        bucket="test-bucket",
    )
    auth = sigv4_authorization(
        creds,
        method="PUT",
        host="test-bucket.s3.us-west-2.amazonaws.com",
        key="fit_zip/user-1/abcdef.zip",
        payload_hash="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        amz_date="20240115T123456Z",
    )
    assert (
        "Signature=0a522294b0ad6389f4efd2a01854b358fa5d58c67d9700a77e3a162e9db195d7" in auth
    )
    assert "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date" in auth


def test_sigv4_with_session_token_signs_security_token() -> None:
    creds = S3Credentials(
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRiCYEXAMPLEKEY",
        session_token="SESSION",
        region="us-west-2",
        bucket="test-bucket",
    )
    auth = sigv4_authorization(
        creds,
        method="PUT",
        host="test-bucket.s3.us-west-2.amazonaws.com",
        key="k.zip",
        payload_hash="2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        amz_date="20240115T123456Z",
    )
    assert ";x-amz-security-token" in auth.split("SignedHeaders=")[1].split(",")[0]


# -- envelope parsing --------------------------------------------------------


def _sts_response(creds_json: dict[str, Any]) -> dict[str, Any]:
    salted = STS_SALT + base64.b64encode(json.dumps(creds_json).encode()).decode()
    return {"code": 200, "msg": "", "data": {"credentials": salted, "v": "2"}}


CRED_S3 = {
    "AccessKeyId": "AKID",
    "SecretAccessKey": "SK",
    "SessionToken": "TOK",
    "Region": "us-east-1",
    "Bucket": "coros-s3",
}


def _client() -> CorosClient:
    return CorosClient(
        region="en", email="e", password="p", timezone_quarters_override=32
    )


@responses.activate
def test_upload_fit_flow_uses_bff_sts_proxy() -> None:
    responses.post(
        "https://teamapi.coros.com/account/login",
        json={"result": "0000", "message": "", "data": {"accessToken": "tok"}},
    )
    responses.get(
        "https://teamapi.coros.com/account/query",
        json={"result": "0000", "message": "", "data": {"userId": "u1", "nickname": "N"}},
    )
    responses.get(
        "https://training.coros.com/api/proxy/oss/sts",
        match_querystring=False,
        json=_sts_response(CRED_S3),
    )
    responses.put(
        re.compile(r"https://coros-s3\.s3\.us-east-1\.amazonaws\.com/fit_zip/u1/.*\.zip"),
        body=b"",
    )
    responses.post(
        "https://teamapi.coros.com/activity/fit/import",
        json={"result": "0000", "message": "", "data": {"id": "job1"}},
    )

    result = _client().upload_fit(b"FITDATA", "peloton-w1.fit")
    assert result.import_id == "job1"
    assert result.md5 == hashlib.md5(b"FITDATA").hexdigest()

    # STS goes through the Training Hub BFF proxy with cookie auth
    sts_calls = [c for c in responses.calls if "training.coros.com" in (c.request.url or "")]
    assert len(sts_calls) == 1
    assert sts_calls[0].request.headers["Cookie"] == "CPL-coros-token=tok"

    # S3 request: SigV4 headers + correct zip body
    s3_req = responses.calls[3].request
    assert s3_req.headers["x-amz-content-sha256"] == hashlib.sha256(
        build_upload_zip(b"FITDATA", result.md5, "peloton-w1.fit")
    ).hexdigest()
    assert s3_req.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    assert s3_req.headers["x-amz-security-token"] == "TOK"
    assert s3_req.headers["Content-Type"] == "application/zip"

    # multipart import registration
    import_req = responses.calls[4].request
    assert isinstance(import_req.body, bytes)
    body = import_req.body.decode("utf-8")
    assert 'name="jsonParameter"' in body
    assert '"oriFileName": "peloton-w1.fit"' in body
    assert '"serviceName": "aws"' in body
    assert '"bucket": "coros-s3"' in body
    assert '"timezone": 32' in body
    assert f'"md5": "{result.md5}"' in body
    assert '"object": "fit_zip/u1/' + result.md5 + '.zip"' in body
    assert import_req.headers["AccessToken"] == "tok"


@responses.activate
def test_sts_falls_back_to_legacy_endpoint() -> None:
    responses.post(
        "https://teamapi.coros.com/account/login",
        json={"result": "0000", "message": "", "data": {"accessToken": "tok"}},
    )
    responses.get(
        "https://teamapi.coros.com/account/query",
        json={"result": "0000", "message": "", "data": {"userId": "u1"}},
    )
    responses.get("https://training.coros.com/api/proxy/oss/sts", status=404, body="404")
    responses.get("https://faq.coros.com/openapi/oss/sts", json=_sts_response(CRED_S3))
    responses.put(
        re.compile(r"https://coros-s3\.s3\.us-east-1\.amazonaws\.com/.*"),
        body=b"",
    )
    responses.post(
        "https://teamapi.coros.com/activity/fit/import",
        json={"result": "0000", "message": "", "data": {"id": "job1"}},
    )
    result = _client().upload_fit(b"F", "peloton-w1.fit")
    assert result.import_id == "job1"
    legacy_calls = [c for c in responses.calls if "faq.coros.com" in (c.request.url or "")]
    assert len(legacy_calls) == 1


@responses.activate
def test_sts_fails_when_all_channels_down() -> None:
    responses.post(
        "https://teamapi.coros.com/account/login",
        json={"result": "0000", "message": "", "data": {"accessToken": "tok"}},
    )
    responses.get("https://training.coros.com/api/proxy/oss/sts", status=404, body="404")
    responses.get("https://faq.coros.com/openapi/oss/sts", status=404, body="404")
    with pytest.raises(CorosHttpError, match="STS failed on all channels"):
        _client().upload_fit(b"F", "x.fit")


@responses.activate
def test_login_2fa_challenge_raises_actionable_error() -> None:
    responses.post(
        "https://teamapi.coros.com/account/login",
        json={
            "result": "0000",
            "message": "OK",
            "data": {
                "twoFactorRequired": True,
                "loginTicket": "ticket-1",
                "appKey": "app-key-1",
                "regionId": 1,
            },
        },
    )
    client = CorosClient(region="en", email="e", password="p")
    with pytest.raises(CorosAuthError, match="two-factor"):
        client.account()


@responses.activate
def test_upload_fit_rejects_cn_region() -> None:
    client = CorosClient(region="cn", access_token="t")
    with pytest.raises(Exception, match="Aliyun"):
        client.upload_fit(b"F", "x.fit")


@responses.activate
def test_login_failure_raises_api_error() -> None:
    responses.post(
        "https://teamapi.coros.com/account/login",
        json={"result": "8001", "message": "bad password"},
    )
    client = CorosClient(region="en", email="e", password="p")
    with pytest.raises(CorosApiError):
        client.account()


def test_no_credentials_raises_auth_error() -> None:
    client = CorosClient(region="en")
    with pytest.raises(CorosAuthError):
        client.account()


@responses.activate
def test_non_json_response_raises_http_error() -> None:
    responses.post(
        "https://teamapi.coros.com/account/login",
        body="<html>gateway</html>",
        status=502,
    )
    client = CorosClient(region="en", email="e", password="p")
    with pytest.raises(CorosHttpError):
        client.account()


@responses.activate
def test_http_error_status_with_success_result() -> None:
    responses.post(
        "https://teamapi.coros.com/account/query",
        json={"result": "0000", "message": "", "data": {}},
        status=500,
    )
    client = CorosClient(region="en", access_token="t")
    with pytest.raises(CorosHttpError):
        client.account()


# -- import jobs -------------------------------------------------------------


def _job_response(status: int, *, filename: str = "peloton-w1.fit", error_size: int = 0) -> str:
    body = {"result": "0000", "message": "", "data": [{"id": "job1", "status": status,
             "originalFilename": filename, "errorSize": error_size}]}
    return json.dumps(body)


@responses.activate
def test_wait_for_import_success() -> None:
    statuses = iter([1, 1, 2])
    responses.add_callback(
        responses.POST,
        "https://teamapi.coros.com/activity/fit/getImportSportList",
        callback=lambda req: (200, {}, _job_response(next(statuses))),
    )
    client = CorosClient(region="en", access_token="t")
    job = client.wait_for_import("job1", timeout_s=5, interval_s=0)
    assert job is not None and job.status == 2


@responses.activate
def test_wait_for_import_error_size() -> None:
    responses.add_callback(
        responses.POST,
        "https://teamapi.coros.com/activity/fit/getImportSportList",
        callback=lambda req: (200, {}, _job_response(1, error_size=3)),
    )
    client = CorosClient(region="en", access_token="t")
    job = client.wait_for_import("job1", timeout_s=5, interval_s=0)
    assert job is not None and job.error_size == 3


@responses.activate
def test_wait_for_import_never_seen_returns_none() -> None:
    responses.post(
        "https://teamapi.coros.com/activity/fit/getImportSportList",
        json={"result": "0000", "message": "", "data": []},
    )
    client = CorosClient(region="en", access_token="t")
    assert client.wait_for_import("ghost", timeout_s=0.2, interval_s=0.05) is None


@responses.activate
def test_imported_filenames_and_oriFileName_fallback() -> None:
    responses.post(
        "https://teamapi.coros.com/activity/fit/getImportSportList",
        json={
            "result": "0000",
            "message": "",
            "data": [
                {"id": "a", "status": 2, "originalFilename": "peloton-w1.fit"},
                {"id": "b", "status": 2, "oriFileName": "peloton-w2.fit"},
            ],
        },
    )
    client = CorosClient(region="en", access_token="t")
    assert client.imported_filenames() == {"peloton-w1.fit", "peloton-w2.fit"}
