from __future__ import annotations

import hashlib
import io
import os
import threading
import unittest
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src.services.attachment_storage import (
    EMPTY_SHA256,
    AttachmentStorageClient,
    AttachmentStorageDisabled,
    canonical_object_key,
    sign_v4_headers,
)


class _S3FixtureHandler(BaseHTTPRequestHandler):
    objects: dict[str, tuple[bytes, str, str]] = {}
    requests: list[dict] = []

    def log_message(self, _format, *_args):
        return

    def _record(self, body: bytes = b"") -> None:
        self.__class__.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payloadHash": self.headers.get("X-Amz-Content-Sha256"),
                "body": body,
            }
        )

    def do_PUT(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        if hashlib.sha256(body).hexdigest() != self.headers.get("X-Amz-Content-Sha256"):
            self.send_response(403)
            self.end_headers()
            return
        if not (self.headers.get("Authorization") or "").startswith("AWS4-HMAC-SHA256 "):
            self.send_response(403)
            self.end_headers()
            return
        self.__class__.objects[self.path] = (
            body,
            self.headers.get("Content-Type", "application/octet-stream"),
            self.headers.get("X-Amz-Meta-Sha256", ""),
        )
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self._record()
        stored = self.__class__.objects.get(self.path)
        if stored is None:
            self.send_response(404)
            self.end_headers()
            return
        body, content_type, sha256 = stored
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.send_header("X-Amz-Meta-Sha256", sha256)
        self.end_headers()

    def do_GET(self):
        self._record()
        stored = self.__class__.objects.get(self.path)
        if stored is None:
            self.send_response(404)
            self.end_headers()
            return
        body, content_type, sha256 = stored
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.send_header("X-Amz-Meta-Sha256", sha256)
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        self._record()
        self.__class__.objects.pop(self.path, None)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


class AttachmentStorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _S3FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        _S3FixtureHandler.objects.clear()
        _S3FixtureHandler.requests.clear()
        self.client = AttachmentStorageClient(
            endpoint_url=self.endpoint,
            bucket_name="todo-test",
            region="us-east-1",
            access_key="test",
            secret_key="test",
            enabled=True,
        )

    def test_matches_official_aws_s3_get_object_signature_vector(self) -> None:
        headers = sign_v4_headers(
            method="GET",
            url="https://examplebucket.s3.amazonaws.com/test.txt",
            region="us-east-1",
            service="s3",
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            payload_hash=EMPTY_SHA256,
            request_time=datetime(2013, 5, 24, tzinfo=UTC),
            headers={"range": "bytes=0-9"},
        )
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/"
            "20130524/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41",
        )

    def test_put_head_and_get_preserve_exact_bytes_hash_and_canonical_path(self) -> None:
        body = b"\x89PNG\r\n\x1a\nprivate-image"
        digest = hashlib.sha256(body).hexdigest()
        key = canonical_object_key(
            "memo-1", "123e4567-e89b-42d3-a456-426614174000"
        )
        self.client.put_object(
            key,
            io.BytesIO(body),
            byte_size=len(body),
            content_type="image/png",
            sha256=digest,
        )
        head = self.client.head_object(key)
        downloaded = self.client.get_object(key, max_bytes=1024)
        self.assertIsNotNone(head)
        self.assertEqual(head.byte_size, len(body))
        self.assertEqual(head.sha256, digest)
        self.assertEqual(downloaded.file.read(), body)
        downloaded.file.close()
        self.assertEqual(
            [request["method"] for request in _S3FixtureHandler.requests],
            ["PUT", "HEAD", "GET"],
        )
        self.assertTrue(
            all(
                request["path"] == f"/todo-test/{key}"
                for request in _S3FixtureHandler.requests
            )
        )
        self.assertTrue(
            all(request["authorization"] for request in _S3FixtureHandler.requests)
        )

    def test_optional_local_s3_round_trip_uses_real_signed_http(self) -> None:
        endpoint = os.getenv("TODO_ATTACHMENT_TEST_S3_ENDPOINT")
        if not endpoint:
            self.skipTest("TODO_ATTACHMENT_TEST_S3_ENDPOINT is not configured")
        from urllib.parse import urlsplit

        if urlsplit(endpoint).hostname not in {"127.0.0.1", "localhost", "::1"}:
            self.fail("Refusing a non-loopback attachment integration endpoint")
        client = AttachmentStorageClient(
            endpoint_url=endpoint,
            bucket_name=os.environ["TODO_ATTACHMENT_TEST_S3_BUCKET"],
            region=os.getenv("TODO_ATTACHMENT_TEST_S3_REGION", "us-east-1"),
            access_key=os.getenv("TODO_ATTACHMENT_TEST_S3_ACCESS_KEY", "test"),
            secret_key=os.getenv("TODO_ATTACHMENT_TEST_S3_SECRET_KEY", "test"),
            enabled=True,
        )
        body = b"todo attachment signed local S3 round trip"
        digest = hashlib.sha256(body).hexdigest()
        attachment_id = str(uuid.uuid4())
        key = canonical_object_key("integration-memo", attachment_id)
        try:
            client.put_object(
                key,
                io.BytesIO(body),
                byte_size=len(body),
                content_type="application/octet-stream",
                sha256=digest,
            )
            head = client.head_object(key)
            downloaded = client.get_object(key, max_bytes=1024)
            self.assertEqual(head.byte_size, len(body))
            self.assertEqual(downloaded.file.read(), body)
            downloaded.file.close()
        finally:
            client.delete_object(key)

    def test_disabled_client_fails_without_network_access(self) -> None:
        client = AttachmentStorageClient(enabled=False)
        with self.assertRaises(AttachmentStorageDisabled):
            client.head_object(
                canonical_object_key(
                    "memo-1", "123e4567-e89b-42d3-a456-426614174000"
                )
            )


if __name__ == "__main__":
    unittest.main()
