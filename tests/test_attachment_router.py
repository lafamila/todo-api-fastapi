from __future__ import annotations

import io
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from starlette.requests import Request

from src.routers.attachments import (
    _read_bounded_body,
    content_disposition,
    normalized_content_type,
    safe_file_name,
    write_zip_archive,
)
from src.routers import attachments as attachment_routes


class AttachmentMetadataTests(unittest.TestCase):
    def test_magic_bytes_are_required_for_inline_images(self) -> None:
        self.assertEqual(
            normalized_content_type("image/png", b"<html>not an image"),
            ("application/octet-stream", False),
        )
        self.assertEqual(
            normalized_content_type("application/octet-stream", b"\x89PNG\r\n\x1a\nrest"),
            ("image/png", True),
        )

    def test_filename_rejects_paths_controls_and_bidi_formatting(self) -> None:
        for value in (
            "../folder\\unsafe.png",
            "unsafe\x00name.png",
            "photo\u202egnp.exe",
            "../..",
        ):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                safe_file_name(value)
        self.assertEqual(safe_file_name("한글 이미지.png"), "한글 이미지.png")

    def test_content_disposition_has_ascii_fallback_and_rfc5987_name(self) -> None:
        header = content_disposition("한글 이미지.png", inline=False)
        self.assertTrue(header.startswith('attachment; filename=".png";'))
        self.assertIn(
            "filename*=UTF-8''%ED%95%9C%EA%B8%80%20%EC%9D%B4%EB%AF%B8%EC%A7%80.png",
            header,
        )


class AttachmentZipTests(unittest.TestCase):
    def test_zip_names_are_unique_on_case_insensitive_filesystems(self) -> None:
        used: set[str] = set()
        names = ["Report.txt", "report.txt", "REPORT (2).TXT", "é.txt", "e\u0301.txt"]
        self.assertEqual(
            [attachment_routes._unique_zip_name(name, used) for name in names],
            ["Report.txt", "report (2).txt", "REPORT (2) (2).TXT", "é.txt", "é (2).txt"],
        )

    def test_zip_uses_safe_unique_basenames_and_preserves_bytes(self) -> None:
        archive = io.BytesIO()
        rows = [
            {"file_name": "../report.txt"},
            {"file_name": "folder/report.txt"},
            {"file_name": "그림.png"},
        ]
        downloads = [
            SimpleNamespace(file=io.BytesIO(b"first")),
            SimpleNamespace(file=io.BytesIO(b"second")),
            SimpleNamespace(file=io.BytesIO(b"image")),
        ]
        write_zip_archive(archive, rows, downloads)
        archive.seek(0)
        with zipfile.ZipFile(archive) as bundle:
            self.assertEqual(
                bundle.namelist(), ["download", "download (2)", "그림.png"]
            )
            self.assertEqual(bundle.read("download"), b"first")
            self.assertEqual(bundle.read("download (2)"), b"second")


class AttachmentBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_body_rejects_bytes_beyond_limit(self) -> None:
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": b"12345", "more_body": False}

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1),
                "server": ("127.0.0.1", 8000),
            },
            receive,
        )
        with patch.object(attachment_routes, "TODO_ATTACHMENT_MAX_FILE_BYTES", 4):
            with self.assertRaises(HTTPException) as raised:
                await _read_bounded_body(request)
        self.assertEqual(raised.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
