"""Run with: python -m unittest src/test_studio.py -v (from top project directory)."""
from __future__ import annotations
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request
from urllib.response import addinfourl
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import github_core as core


def example_zip():
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("test-main/README.md", "hello world")
    return mem.getvalue()


class FakeResponse(io.BytesIO):
    def __init__(self, payload, status=200, headers=None):
        super().__init__(payload)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(payload))}


class GithubStudioTests(unittest.TestCase):
    def test_all_uploaded_urls(self):
        text = (Path(__file__).resolve().parent.parent / "Example_List" / "GITHUB_LINKS_ONLY.txt").read_text()
        links, rejected = core.parse_bulk(text)
        self.assertEqual(len(links), 94)
        self.assertEqual(sum(link.kind == "repo" for link in links), 93)
        self.assertEqual(sum(link.kind == "profile" for link in links), 1)
        self.assertEqual(rejected, [])

    def test_formats(self):
        examples = {
            "https://github.com/psf/requests": ("repo", "psf", "requests", ""),
            "PSF/requests": ("repo", "PSF", "requests", ""),
            "https://github.com/psf/requests.git": ("repo", "psf", "requests", ""),
            "https://github.com/psf/requests/archive/refs/heads/main.zip": ("repo", "psf", "requests", "main"),
            "https://codeload.github.com/psf/requests/zip/refs/heads/main": ("repo", "psf", "requests", "main"),
            "https://api.github.com/repos/psf/requests/zipball/main": ("repo", "psf", "requests", "main"),
            "https://github.com/psf": ("profile", "psf", "", ""),
            "https://github.com/psf/requests/releases/download/v1/thing.zip": ("direct", "psf", "requests", ""),
            "https://example.com/thing.zip": ("direct", "", "", ""),
        }
        for text, want in examples.items():
            link = core.parse_link(text)
            self.assertIsNotNone(link, text)
            self.assertEqual((link.kind, link.owner, link.repo, link.ref), want, text)

    def test_no_insecure_or_other_websites(self):
        for raw in ["http://github.com/psf/requests", "https://example.com/", "https://example.com/hello.txt", "", "javascript:alert(1)"]:
            self.assertIsNone(core.parse_link(raw), raw)

    def test_duplicates_and_telegram_export(self):
        links, rejected = core.parse_bulk("hello (https://github.com/psf/requests) there\nhttps://github.com/PSF/requests/\nhttps://example.com/fail\n")
        self.assertEqual(len(links), 1)
        self.assertEqual(len(rejected), 1)

    def test_safe_file_paths(self):
        with tempfile.TemporaryDirectory() as d:
            link = core.parse_link("https://github.com/psf/requests")
            path = core.archive_path(link, Path(d))
            self.assertTrue(str(path).endswith("Repositories/psf/requests/requests__default.zip"))
            direct1 = core.archive_path(core.parse_link("https://example.com/a.zip?version=1"), Path(d))
            direct2 = core.archive_path(core.parse_link("https://example.com/a.zip?version=2"), Path(d))
            self.assertNotEqual(direct1, direct2)

    def test_archive_download_and_skip(self):
        payload = example_zip()
        with tempfile.TemporaryDirectory() as d:
            link = core.parse_link("https://github.com/psf/requests")
            with patch.object(core.HTTP, "open", side_effect=lambda req, timeout: FakeResponse(payload)) as mock:
                state, dest, count = core.download_archive(link, Path(d))
                self.assertEqual(state, "Completed")
                self.assertEqual(count, len(payload))
                self.assertTrue(zipfile.is_zipfile(dest))
                state, same, count = core.download_archive(link, Path(d))
                self.assertEqual(state, "Skipped existing")
                self.assertEqual(mock.call_count, 1)

    def test_resume_range(self):
        payload = example_zip()
        link = core.parse_link("https://github.com/python/cpython")
        with tempfile.TemporaryDirectory() as d:
            target = core.archive_path(link, Path(d))
            target.parent.mkdir(parents=True)
            partial_len = len(payload) // 2
            target.with_name(target.name + ".part").write_bytes(payload[:partial_len])
            def fake(req, timeout):
                self.assertEqual(req.get_header("Range"), f"bytes={partial_len}-")
                return FakeResponse(payload[partial_len:], 206, {"Content-Length": str(len(payload) - partial_len), "Content-Range": f"bytes {partial_len}-{len(payload)-1}/{len(payload)}"})
            with patch.object(core.HTTP, "open", side_effect=fake):
                state, dest, count = core.download_archive(link, d)
            self.assertEqual((state, count), ("Completed", len(payload)))
            self.assertEqual(dest.read_bytes(), payload)

    def test_restart_when_range_ignored(self):
        payload = example_zip()
        link = core.parse_link("https://github.com/python/cpython")
        with tempfile.TemporaryDirectory() as d:
            target = core.archive_path(link, Path(d))
            target.parent.mkdir(parents=True)
            target.with_name(target.name + ".part").write_bytes(payload[:40])
            with patch.object(core.HTTP, "open", side_effect=lambda req, timeout: FakeResponse(payload)):
                state, dest, count = core.download_archive(link, d)
            self.assertEqual(dest.read_bytes(), payload)

    def test_invalid_zip_reported(self):
        link = core.parse_link("https://github.com/psf/requests")
        with tempfile.TemporaryDirectory() as d:
            with patch.object(core.HTTP, "open", side_effect=lambda req, timeout: FakeResponse(b"<html>Access denied</html>")):
                with self.assertRaisesRegex(core.GitHubError, "not a ZIP"):
                    core.download_archive(link, d)

    def test_stopped_preserves_partial(self):
        link = core.parse_link("https://github.com/psf/requests")
        event = __import__("threading").Event()
        event.set()
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(core.DownloadCancelled):
                core.download_archive(link, d, stop=event)

    def test_report(self):
        with tempfile.TemporaryDirectory() as d:
            core.save_manifest(d, [{"name": "test", "url": "https://github.com/a/b", "status": "Failed", "path": "", "size": 0, "error": "missing"}])
            self.assertIn("missing", (Path(d)/"FAILED_LINKS.txt").read_text())
            self.assertIn("test", (Path(d)/"DOWNLOAD_MANIFEST.csv").read_text(encoding="utf-8-sig"))

    def test_redirect_strips_token_for_other_host(self):
        redirect = core.SafeRedirect()
        req = Request("https://api.github.com/repos/psf/requests/zipball", headers={"Authorization": "Bearer SECRET", "User-Agent": "test"})
        other = redirect.redirect_request(req, None, 302, "Found", {}, "https://codeload.github.com/psf/requests/legacy.zip/main")
        self.assertNotIn("authorization", [x.lower() for x in other.headers])
        with self.assertRaises(ValueError):
            redirect.redirect_request(req, None, 302, "Found", {}, "http://example.com/token")


if __name__ == "__main__":
    unittest.main()
