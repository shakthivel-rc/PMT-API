"""Pure Python Azure Blob Storage REST client with SharedKey HMAC-SHA256.

Operates over standard library urllib without requiring external heavy SDK dependencies.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import quote


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


class AzureBlobClient:
    """REST client for Azure Blob Storage using SharedKey authentication."""

    def __init__(
        self,
        account_name: str,
        account_key: str,
        container_name: str,
        timeout: float = 10.0,
    ) -> None:
        self.account_name = account_name.strip()
        self.account_key = account_key.strip()
        self.container_name = container_name.strip()
        self.timeout = timeout

    def _build_auth_headers(
        self,
        method: str,
        resource_path: str,
        content_length: int = 0,
        content_type: str = "",
        custom_headers: dict[str, str] | None = None,
        query_params: dict[str, str] | None = None,
    ) -> dict[str, str]:
        rfc1123_date = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        ms_headers = {"x-ms-date": rfc1123_date, "x-ms-version": "2020-10-02"}
        if custom_headers:
            ms_headers.update(custom_headers)

        sorted_keys = sorted(k.lower() for k in ms_headers)
        canon_headers = "".join(f"{k}:{ms_headers[k]}\n" for k in sorted_keys)

        canon_resource = f"/{self.account_name}{resource_path}"
        if query_params:
            for k in sorted(query_params.keys()):
                canon_resource += f"\n{k.lower()}:{query_params[k]}"

        str_content_len = str(content_length) if content_length > 0 else ""
        string_to_sign = (
            f"{method}\n"
            f"\n"  # Content-Encoding
            f"\n"  # Content-Language
            f"{str_content_len}\n"
            f"\n"  # Content-MD5
            f"{content_type}\n"
            f"\n"  # Date
            f"\n"  # If-Modified-Since
            f"\n"  # If-Match
            f"\n"  # If-None-Match
            f"\n"  # If-Unmodified-Since
            f"\n"  # Range
            f"{canon_headers}"
            f"{canon_resource}"
        )

        try:
            decoded_key = base64.b64decode(self.account_key)
        except Exception as exc:
            raise ValueError(f"Invalid base64 Azure account key: {exc!s}") from exc

        signature = base64.b64encode(
            hmac.new(decoded_key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")

        headers = {
            "Authorization": f"SharedKey {self.account_name}:{signature}",
            "x-ms-date": rfc1123_date,
            "x-ms-version": "2020-10-02",
        }
        if custom_headers:
            headers.update(custom_headers)
        if content_type:
            headers["Content-Type"] = content_type
        if content_length > 0:
            headers["Content-Length"] = str(content_length)
        return headers

    def upload_blob(
        self,
        blob_name: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, str]:
        encoded_blob = quote(blob_name.lstrip("/"), safe="/-_.~")
        resource_path = f"/{self.container_name}/{encoded_blob}"
        url = f"https://{self.account_name}.blob.core.windows.net{resource_path}"

        headers = self._build_auth_headers(
            "PUT",
            resource_path=resource_path,
            content_length=len(content),
            content_type=content_type,
            custom_headers={"x-ms-blob-type": "BlockBlob"},
        )

        resp = _send_request("PUT", url, headers, content=content, timeout=self.timeout)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Azure blob upload failed ({resp.status_code}): {resp.text[:200]}"
            )
        return {"url": url, "blob_name": blob_name, "etag": resp.headers.get("etag", "")}

    def download_blob(self, blob_name: str) -> bytes:
        encoded_blob = quote(blob_name.lstrip("/"), safe="/-_.~")
        resource_path = f"/{self.container_name}/{encoded_blob}"
        url = f"https://{self.account_name}.blob.core.windows.net{resource_path}"

        headers = self._build_auth_headers("GET", resource_path=resource_path)
        resp = _send_request("GET", url, headers, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Azure blob download failed ({resp.status_code}): {resp.text[:200]}"
            )
        return resp.content

    def delete_blob(self, blob_name: str) -> bool:
        encoded_blob = quote(blob_name.lstrip("/"), safe="/-_.~")
        resource_path = f"/{self.container_name}/{encoded_blob}"
        url = f"https://{self.account_name}.blob.core.windows.net{resource_path}"

        headers = self._build_auth_headers("DELETE", resource_path=resource_path)
        resp = _send_request("DELETE", url, headers, timeout=self.timeout)
        return resp.status_code in (200, 202)

    def test_connection(self) -> tuple[bool, str]:
        """Test container accessibility with container properties request."""
        resource_path = f"/{self.container_name}"
        url = (
            f"https://{self.account_name}.blob.core.windows.net{resource_path}?restype=container"
        )
        try:
            headers = self._build_auth_headers(
                "GET", resource_path=resource_path, query_params={"restype": "container"}
            )
            resp = _send_request("GET", url, headers, timeout=self.timeout)
            if resp.status_code in (200, 204):
                return True, "Successfully connected to Azure Blob Storage container."
            if resp.status_code == 403:
                return (
                    False,
                    "Access denied: check Azure Storage Account Name and Account Key.",
                )
            if resp.status_code == 404:
                return False, f"Container '{self.container_name}' does not exist."
            return False, f"Azure connection test failed with HTTP {resp.status_code}."
        except Exception as exc:
            return False, f"Could not reach Azure endpoint: {exc!s}"
