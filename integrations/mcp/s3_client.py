"""Pure Python AWS S3 client implementing AWS Signature Version 4.

Operates over standard library urllib without requiring external heavy SDK dependencies.
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import quote, urlparse


def _get_signature_key(key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = hmac.new(
        ("AWS4" + key).encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256
    ).digest()
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    return k_signing


class _HttpResponse:
    def __init__(self, status_code: int, headers: dict[str, str], content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def _send_request(
    method: str,
    url: str,
    headers: dict[str, str],
    content: bytes | None = None,
    timeout: float = 10.0,
) -> _HttpResponse:
    if not (url.startswith("https://") or url.startswith("http://")):
        raise ValueError(f"URL scheme not allowed: {url}")

    req = urllib.request.Request(
        url,
        data=content if (content is not None and method in ("PUT", "POST")) else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            return _HttpResponse(resp.status, resp_headers, resp.read())
    except urllib.error.HTTPError as exc:
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        return _HttpResponse(exc.code, resp_headers, exc.read())


class S3Client:
    """REST client for AWS S3 and S3-compatible object storage APIs."""

    def __init__(
        self,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.bucket = bucket.strip()
        self.access_key_id = access_key_id.strip()
        self.secret_access_key = secret_access_key.strip()
        self.region = region.strip() or "us-east-1"
        self.endpoint_url = endpoint_url.strip() if endpoint_url else None
        self.timeout = timeout

    def _resolve_url_and_host(self, key: str) -> tuple[str, str, str]:
        normalized_key = key.lstrip("/")
        encoded_key = quote(normalized_key, safe="/-_.~")

        if self.endpoint_url:
            parsed = urlparse(self.endpoint_url)
            host = parsed.netloc
            base_path = parsed.path.rstrip("/")
            path = f"{base_path}/{self.bucket}/{encoded_key}"
            url = f"{parsed.scheme}://{host}{path}"
            return url, host, path

        host = f"{self.bucket}.s3.{self.region}.amazonaws.com"
        path = f"/{encoded_key}"
        url = f"https://{host}{path}"
        return url, host, path

    def _build_signed_headers(
        self, method: str, path: str, host: str, payload: bytes, content_type: str | None = None
    ) -> dict[str, str]:
        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        payload_hash = hashlib.sha256(payload).hexdigest()

        canonical_uri = path or "/"
        canonical_querystring = ""

        signed_headers_list = ["host", "x-amz-content-sha256", "x-amz-date"]
        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        if content_type:
            signed_headers_list.append("content-type")
            canonical_headers = (
                f"content-type:{content_type}\n"
                f"host:{host}\n"
                f"x-amz-content-sha256:{payload_hash}\n"
                f"x-amz-date:{amz_date}\n"
            )

        signed_headers = ";".join(sorted(signed_headers_list))

        canonical_request = (
            f"{method}\n"
            f"{canonical_uri}\n"
            f"{canonical_querystring}\n"
            f"{canonical_headers}\n"
            f"{signed_headers}\n"
            f"{payload_hash}"
        )

        algorithm = "AWS4-HMAC-SHA256"
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = (
            f"{algorithm}\n"
            f"{amz_date}\n"
            f"{credential_scope}\n"
            f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
        )

        signing_key = _get_signature_key(
            self.secret_access_key, date_stamp, self.region, "s3"
        )
        signature = hmac.new(
            signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        auth_header = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        req_headers = {
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": auth_header,
        }
        if content_type:
            req_headers["Content-Type"] = content_type
        return req_headers

    def upload_file(
        self, key: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> dict[str, str]:
        url, host, path = self._resolve_url_and_host(key)
        headers = self._build_signed_headers("PUT", path, host, content, content_type)
        resp = _send_request("PUT", url, headers, content=content, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"S3 upload failed ({resp.status_code}): {resp.text[:200]}"
            )
        return {"url": url, "key": key, "etag": resp.headers.get("etag", "")}

    def download_file(self, key: str) -> bytes:
        url, host, path = self._resolve_url_and_host(key)
        headers = self._build_signed_headers("GET", path, host, b"")
        resp = _send_request("GET", url, headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(
                f"S3 download failed ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.content

    def delete_file(self, key: str) -> bool:
        url, host, path = self._resolve_url_and_host(key)
        headers = self._build_signed_headers("DELETE", path, host, b"")
        resp = _send_request("DELETE", url, headers, timeout=self.timeout)
        return resp.status_code in (200, 204)

    def test_connection(self) -> tuple[bool, str]:
        """Test bucket accessibility with a HEAD request."""
        url, host, _ = self._resolve_url_and_host("")
        root_path = "/"
        if self.endpoint_url:
            parsed = urlparse(self.endpoint_url)
            root_path = f"{parsed.path.rstrip('/')}/{self.bucket}"
            url = f"{parsed.scheme}://{host}{root_path}"
        else:
            url = f"https://{host}/"

        headers = self._build_signed_headers("HEAD", root_path, host, b"")
        try:
            resp = _send_request("HEAD", url, headers, timeout=self.timeout)
            if resp.status_code in (200, 204):
                return True, "Successfully connected to AWS S3 bucket."
            if resp.status_code == 403:
                return False, "Access denied: verify AWS Access Key and Secret Key permissions."
            if resp.status_code == 404:
                return False, f"Bucket '{self.bucket}' does not exist or region is incorrect."
            return False, f"S3 connection test failed with HTTP {resp.status_code}."
        except Exception as exc:
            return False, f"Could not reach S3 endpoint: {exc!s}"
