"""Private S3-compatible storage using only Python's standard library."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from tempfile import SpooledTemporaryFile
from typing import BinaryIO
from urllib.parse import quote, unquote, urlencode, urlsplit

try:
    from ..config import (
        AWS_ACCESS_KEY_ID,
        AWS_SECRET_ACCESS_KEY,
        S3_BUCKET_NAME,
        S3_ENDPOINT_URL,
        S3_REGION,
        STORAGE_BACKEND,
        TODO_ATTACHMENT_MAX_COUNT_PER_MEMO,
        TODO_ATTACHMENT_MAX_FILE_BYTES,
        TODO_ATTACHMENT_MAX_ZIP_BYTES,
        TODO_ATTACHMENT_SPOOL_BYTES,
        TODO_ATTACHMENT_STORAGE_ENABLED,
        TODO_ATTACHMENT_STORAGE_TIMEOUT_SECONDS,
        TODO_ATTACHMENT_UPLOAD_STALE_SECONDS,
    )
except ImportError:  # pragma: no cover
    from config import (
        AWS_ACCESS_KEY_ID,
        AWS_SECRET_ACCESS_KEY,
        S3_BUCKET_NAME,
        S3_ENDPOINT_URL,
        S3_REGION,
        STORAGE_BACKEND,
        TODO_ATTACHMENT_MAX_COUNT_PER_MEMO,
        TODO_ATTACHMENT_MAX_FILE_BYTES,
        TODO_ATTACHMENT_MAX_ZIP_BYTES,
        TODO_ATTACHMENT_SPOOL_BYTES,
        TODO_ATTACHMENT_STORAGE_ENABLED,
        TODO_ATTACHMENT_STORAGE_TIMEOUT_SECONDS,
        TODO_ATTACHMENT_UPLOAD_STALE_SECONDS,
    )


EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
CANONICAL_KEY_RE = re.compile(
    r"^todo/memos/[0-9A-Za-z][0-9A-Za-z._:-]{0,49}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class AttachmentStorageError(RuntimeError):
    """Storage operation failed without exposing upstream response bodies."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AttachmentStorageDisabled(AttachmentStorageError):
    pass


@dataclass(frozen=True)
class StoredObjectHead:
    byte_size: int
    content_type: str
    sha256: str | None


@dataclass
class DownloadedObject:
    file: BinaryIO
    byte_size: int
    content_type: str


def canonical_object_key(memo_id: str, attachment_id: str) -> str:
    key = f"todo/memos/{memo_id}/{attachment_id}"
    if not CANONICAL_KEY_RE.fullmatch(key):
        raise ValueError("Invalid canonical attachment object key")
    return key


def validate_storage_configuration() -> None:
    if not TODO_ATTACHMENT_STORAGE_ENABLED:
        raise AttachmentStorageDisabled("Memo attachment storage is not configured")
    if STORAGE_BACKEND not in {"localstack", "r2"}:
        raise AttachmentStorageError("STORAGE_BACKEND must be localstack or r2")
    parsed = urlsplit(S3_ENDPOINT_URL)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AttachmentStorageError("S3_ENDPOINT_URL must be an absolute HTTP URL")
    if STORAGE_BACKEND == "r2":
        if parsed.scheme != "https" or not parsed.hostname.endswith(
            ".r2.cloudflarestorage.com"
        ):
            raise AttachmentStorageError(
                "R2 storage requires the private Cloudflare R2 S3 API endpoint"
            )
        if S3_REGION != "auto":
            raise AttachmentStorageError("R2 storage requires S3_REGION=auto")
        if AWS_ACCESS_KEY_ID == "test" or AWS_SECRET_ACCESS_KEY == "test":
            raise AttachmentStorageError("R2 storage requires non-test credentials")
    if not S3_BUCKET_NAME or "/" in S3_BUCKET_NAME:
        raise AttachmentStorageError("S3_BUCKET_NAME is invalid")
    if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
        raise AttachmentStorageError("S3 storage credentials are missing")
    if TODO_ATTACHMENT_STORAGE_TIMEOUT_SECONDS <= 0:
        raise AttachmentStorageError("Storage timeout must be positive")
    if (
        TODO_ATTACHMENT_MAX_FILE_BYTES <= 0
        or TODO_ATTACHMENT_MAX_COUNT_PER_MEMO <= 0
        or TODO_ATTACHMENT_MAX_ZIP_BYTES < TODO_ATTACHMENT_MAX_FILE_BYTES
        or TODO_ATTACHMENT_SPOOL_BYTES <= 0
        or TODO_ATTACHMENT_UPLOAD_STALE_SECONDS <= 0
    ):
        raise AttachmentStorageError("Attachment storage limits are invalid")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs: list[tuple[str, str]] = []
    for part in query.split("&"):
        key, separator, value = part.partition("=")
        pairs.append((key, value if separator else ""))
    return urlencode(sorted(pairs), doseq=True, quote_via=quote, safe="-_.~")


def sign_v4_headers(
    *,
    method: str,
    url: str,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
    payload_hash: str,
    request_time: datetime,
    headers: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return normalized headers including an AWS Signature V4 Authorization."""
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Cannot sign a relative URL")
    amz_date = request_time.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]
    normalized = {
        key.strip().lower(): " ".join(str(value).strip().split())
        for key, value in (headers or {}).items()
    }
    normalized["host"] = parsed.netloc
    normalized["x-amz-content-sha256"] = payload_hash
    normalized["x-amz-date"] = amz_date
    signed_names = sorted(normalized)
    canonical_headers = "".join(f"{name}:{normalized[name]}\n" for name in signed_names)
    signed_headers = ";".join(signed_names)
    canonical_request = "\n".join(
        (
            method.upper(),
            quote(unquote(parsed.path or "/"), safe="/-_.~"),
            _canonical_query(parsed.query),
            canonical_headers,
            signed_headers,
            payload_hash,
        )
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        )
    )
    date_key = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    region_key = _hmac_sha256(date_key, region)
    service_key = _hmac_sha256(region_key, service)
    signing_key = _hmac_sha256(service_key, "aws4_request")
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    normalized["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "-".join(part.capitalize() for part in key.split("-")): value
        for key, value in normalized.items()
    }


class AttachmentStorageClient:
    def __init__(
        self,
        *,
        endpoint_url: str = S3_ENDPOINT_URL,
        bucket_name: str = S3_BUCKET_NAME,
        region: str = S3_REGION,
        access_key: str = AWS_ACCESS_KEY_ID,
        secret_key: str = AWS_SECRET_ACCESS_KEY,
        timeout_seconds: int = TODO_ATTACHMENT_STORAGE_TIMEOUT_SECONDS,
        enabled: bool = TODO_ATTACHMENT_STORAGE_ENABLED,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.bucket_name = bucket_name
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout_seconds = timeout_seconds
        self.enabled = enabled

    def _validate(self) -> None:
        if not self.enabled:
            raise AttachmentStorageDisabled("Memo attachment storage is not configured")
        if not self.access_key or not self.secret_key:
            raise AttachmentStorageError("S3 storage credentials are missing")
        parsed = urlsplit(self.endpoint_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise AttachmentStorageError("Invalid S3 endpoint")
        if not self.bucket_name or "/" in self.bucket_name:
            raise AttachmentStorageError("Invalid S3 bucket")

    def _object_url(self, key: str) -> str:
        if not CANONICAL_KEY_RE.fullmatch(key):
            raise AttachmentStorageError("Refusing non-canonical attachment key")
        return (
            f"{self.endpoint_url}/{quote(self.bucket_name, safe='-_.~')}/"
            f"{quote(key, safe='/-_.~')}"
        )

    def _request(
        self,
        method: str,
        key: str,
        *,
        payload_hash: str = EMPTY_SHA256,
        body: BinaryIO | None = None,
        byte_size: int = 0,
        content_type: str | None = None,
        sha256_metadata: str | None = None,
    ) -> http.client.HTTPResponse:
        self._validate()
        url = self._object_url(key)
        headers: dict[str, str] = {}
        if content_type:
            headers["content-type"] = content_type
        if sha256_metadata:
            headers["x-amz-meta-sha256"] = sha256_metadata
        signed = sign_v4_headers(
            method=method,
            url=url,
            region=self.region,
            service="s3",
            access_key=self.access_key,
            secret_key=self.secret_key,
            payload_hash=payload_hash,
            request_time=datetime.now(UTC),
            headers=headers,
        )
        if body is not None:
            signed["Content-Length"] = str(byte_size)
        parsed = urlsplit(url)
        connection_class = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            parsed.hostname, parsed.port, timeout=self.timeout_seconds
        )
        try:
            connection.request(
                method,
                parsed.path + (f"?{parsed.query}" if parsed.query else ""),
                body=body,
                headers=signed,
            )
            return connection.getresponse()
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            connection.close()
            raise AttachmentStorageError("Attachment storage is unavailable") from exc

    @staticmethod
    def _require_success(response: http.client.HTTPResponse, operation: str) -> None:
        if 200 <= response.status < 300:
            return
        status = response.status
        response.read(64 * 1024)
        response.close()
        raise AttachmentStorageError(
            f"Attachment storage {operation} failed", status_code=status
        )

    def put_object(
        self,
        key: str,
        file_obj: BinaryIO,
        *,
        byte_size: int,
        content_type: str,
        sha256: str,
    ) -> None:
        file_obj.seek(0)
        response = self._request(
            "PUT",
            key,
            payload_hash=sha256,
            body=file_obj,
            byte_size=byte_size,
            content_type=content_type,
            sha256_metadata=sha256,
        )
        self._require_success(response, "upload")
        response.read()
        response.close()

    def head_object(self, key: str) -> StoredObjectHead | None:
        response = self._request("HEAD", key)
        if response.status == 404:
            response.close()
            return None
        self._require_success(response, "metadata read")
        try:
            return StoredObjectHead(
                byte_size=int(response.headers.get("Content-Length", "0")),
                content_type=response.headers.get(
                    "Content-Type", "application/octet-stream"
                ),
                sha256=response.headers.get("x-amz-meta-sha256"),
            )
        finally:
            response.close()

    def get_object(self, key: str, *, max_bytes: int) -> DownloadedObject:
        response = self._request("GET", key)
        self._require_success(response, "download")
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        expected = int(response.headers.get("Content-Length", "0") or "0")
        if expected > max_bytes:
            response.close()
            raise AttachmentStorageError("Stored attachment exceeds the download limit")
        output = SpooledTemporaryFile(
            max_size=TODO_ATTACHMENT_SPOOL_BYTES, mode="w+b"
        )
        total = 0
        try:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise AttachmentStorageError(
                        "Stored attachment exceeds the download limit"
                    )
                output.write(chunk)
        except Exception:
            output.close()
            raise
        finally:
            response.close()
        output.seek(0)
        return DownloadedObject(output, total, content_type)

    def delete_object(self, key: str) -> None:
        """Delete an object; used by isolated storage verification/cleanup."""
        response = self._request("DELETE", key)
        self._require_success(response, "delete")
        response.read()
        response.close()


_storage_client: AttachmentStorageClient | None = None


def get_attachment_storage() -> AttachmentStorageClient:
    global _storage_client
    if _storage_client is None:
        validate_storage_configuration()
        _storage_client = AttachmentStorageClient()
    return _storage_client


def set_attachment_storage_for_tests(client: AttachmentStorageClient | None) -> None:
    global _storage_client
    _storage_client = client
