"""Small SigV4 helper for AWS APIs missing from the desktop's old AWS CLI.

Only static shared-file or environment credentials are supported. No credential
is printed or returned in a response. Normal deployments can also use the CLI.
"""

from __future__ import annotations

import configparser
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def credentials(profile: str = "default") -> tuple[str, str, str | None]:
    if profile == "default" and os.environ.get("AWS_ACCESS_KEY_ID"):
        return (
            os.environ["AWS_ACCESS_KEY_ID"],
            os.environ["AWS_SECRET_ACCESS_KEY"],
            os.environ.get("AWS_SESSION_TOKEN"),
        )
    parser = configparser.ConfigParser()
    parser.read(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", str(Path.home() / ".aws" / "credentials")))
    if profile not in parser:
        raise RuntimeError(f"No static shared credentials for AWS profile {profile!r}")
    section = parser[profile]
    return section["aws_access_key_id"], section["aws_secret_access_key"], section.get("aws_session_token")


def request(
    service: str,
    region: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    profile: str = "default",
    query: dict | None = None,
    host: str | None = None,
    signing_service: str | None = None,
    timeout: int = 60,
) -> dict:
    """Make one signed JSON request, raising a sanitized error on HTTP failure."""
    access, secret, token = credentials(profile)
    hostname = host or f"{service}.{region}.amazonaws.com"
    service_name = signing_service or service
    now = dt.datetime.now(dt.timezone.utc)
    timestamp, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
    digest = hashlib.sha256(body).hexdigest()
    query_string = urlencode(sorted((query or {}).items()), quote_via=quote, safe="~")
    headers = {"content-type": "application/json", "host": hostname, "x-amz-date": timestamp}
    if token:
        headers["x-amz-security-token"] = token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{name}:{value.strip()}\n" for name, value in sorted(headers.items()))
    request_uri = quote(path, safe="/~")
    canonical_uri = quote(request_uri, safe="/~")
    canonical = "\n".join((method, canonical_uri, query_string, canonical_headers, signed_headers, digest))
    scope = f"{date}/{region}/{service_name}/aws4_request"
    string_to_sign = "AWS4-HMAC-SHA256\n" + timestamp + "\n" + scope + "\n" + hashlib.sha256(canonical.encode()).hexdigest()

    def sign(key: bytes, value: str) -> bytes:
        return hmac.new(key, value.encode(), hashlib.sha256).digest()

    signing_key = sign(sign(sign(sign(("AWS4" + secret).encode(), date), region), service_name), "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    url = f"https://{hostname}{request_uri}" + (f"?{query_string}" if query_string else "")
    req = Request(url, data=body if payload is not None else None, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            content = response.read()
            return json.loads(content) if content else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1200]
        raise RuntimeError(f"AWS {service} HTTP {exc.code}: {detail}") from None
