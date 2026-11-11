#!/usr/bin/env python3
"""Offline tests for hltv-demo-fetch (no network required).

Run:
  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import gzip
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from hltv_demo_fetch import archive, cli, net  # noqa: E402
from hltv_demo_fetch.archive import extract_demos  # noqa: E402
from hltv_demo_fetch.manifest import Manifest, demo_key  # noqa: E402
from hltv_demo_fetch.pipeline import (  # noqa: E402
    Job,
    close_jobs,
    local_jobs,
    run_jobs,
)
from hltv_demo_fetch import hltv  # noqa: E402
from hltv_demo_fetch.stealth import (  # noqa: E402
    BrowserSession,
    HltvFetcher,
    StealthConfig,
    _dedupe_cookies,
    classify_page,
    cookie_header_to_cookies,
    extract_cdn_url,
    load_state,
    save_state,
)
from hltv_demo_fetch.util import safe_name  # noqa: E402


class UtilTests(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(safe_name("Spirit vs FUT/2026"), "Spirit vs FUT_2026")
        self.assertEqual(safe_name(""), "demo")
        self.assertLessEqual(len(safe_name("x" * 200, max_len=20)), 20)
        self.assertEqual(safe_name("CON"), "_CON")

    def test_netscape_cookies(self):
        from hltv_demo_fetch.util import read_netscape_cookies

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cookies.txt"
            path.write_text(
                "# Netscape HTTP Cookie File\n"
                ".hltv.org\tTRUE\t/\tTRUE\t0\tcf_clearance\tabc\n"
                "#HttpOnly_.hltv.org\tTRUE\t/\tTRUE\t0\tautologin\tdef\n",
                encoding="utf-8",
            )
            self.assertEqual(
                read_netscape_cookies(path),
                "cf_clearance=abc; autologin=def",
            )


class HltvParseTests(unittest.TestCase):
    def test_page_fetch_passes_cookie_file(self):
        with mock.patch.object(hltv, "request_text", return_value="ok") as mocked:
            self.assertEqual(
                hltv.fetch_hltv_text(
                    "https://www.hltv.org/results", cookie_file="/tmp/cookies.txt"
                ),
                "ok",
            )
        self.assertEqual(mocked.call_args.kwargs["cookie_file"], "/tmp/cookies.txt")

    def test_match_page_demo_link(self):
        page = """
        [Demo download](https://www.hltv.org/matches/2399724/x-y)
        [Click here](https://www.hltv.org/download/demo/113158)
        """
        self.assertEqual(hltv.parse_demo_ids(page), ["113158"])

    def test_match_page_html_demo_link(self):
        page = '<a href="/download/demo/113158">Download</a>'
        self.assertEqual(hltv.parse_demo_ids(page), ["113158"])

    def test_results_dates(self):
        page = """
        Results for November 1st 2026
        [A vs B bo3](https://www.hltv.org/matches/2399986/a-vs-b-event)
        [C vs D bo5](https://www.hltv.org/matches/2399985/c-vs-d-event)

        Results for October 31st 2026
        [E vs F bo3](https://www.hltv.org/matches/2399901/e-vs-f-event)
        """
        matches = hltv.parse_results_page(page)
        self.assertEqual(len(matches), 3)
        self.assertEqual(matches[0].date, "2026-11-01")
        self.assertEqual(matches[2].date, "2026-10-31")
        self.assertEqual(matches[0].match_id, "2399986")

    def test_relative_links(self):
        page = '<div class="result-con"><a href="/matches/2399759/nemiga-vs-furia">x</a></div>'
        matches = hltv.parse_match_links(page)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].match_id, "2399759")

    def test_jobs_from_match_url(self):
        page = "[Click](https://www.hltv.org/download/demo/113158)"
        with mock.patch.object(hltv, "fetch_hltv_text", return_value=page):
            jobs = hltv.jobs(
                match_urls=["https://www.hltv.org/matches/2399724/legacy-vs-nemiga"],
                limit=5,
                engine="stdlib",
            )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source, "hltv")
        self.assertEqual(jobs[0].match_id, "2399724")
        self.assertEqual(jobs[0].demo_id, "113158")
        self.assertTrue(jobs[0].url.endswith("/download/demo/113158"))
        self.assertIsNotNone(jobs[0].fallback_url_resolver)

    def test_extract_cdn_url_from_jina_challenge(self):
        text = (
            "Title: Attention Required!\n"
            "Final URL: https://r2-demos.hltv.org/demos/132423/"
            "pgl-masters-bucharest-2026-legacy-vs-nemiga-bo3-abc.rar\n"
        )
        self.assertEqual(
            hltv.extract_cdn_url(text),
            "https://r2-demos.hltv.org/demos/132423/"
            "pgl-masters-bucharest-2026-legacy-vs-nemiga-bo3-abc.rar",
        )

    def test_results_ignore_sidebar_and_assign_featured_date(self):
        page = """
        [Featured A vs B bo3](https://www.hltv.org/matches/2399986/a-vs-b-event)
        Results for November 1st 2026
        [C vs D bo3](https://www.hltv.org/matches/2399985/c-vs-d-event)
        [sidebar E vs F 19](https://www.hltv.org/matches/2399901/e-vs-f-event)
        """
        matches = hltv.parse_results_page(page)
        self.assertEqual([match.match_id for match in matches], ["2399986", "2399985"])
        self.assertEqual(matches[0].date, "2026-11-01")


class FakeHltvFetcher:
    """Minimal stand-in for HltvFetcher in offline tests."""

    def __init__(self, page: str = ""):
        self.page = page
        self.closed = False
        self.downloads: list[str] = []

    def get_text(self, url: str, **_kwargs) -> str:
        return self.page

    def resolve_demo_url(self, demo_id: str) -> str:
        return f"https://r2-demos.hltv.org/demos/{demo_id}/test.rar"

    def download_demo(self, demo_id: str, *, destination, timeout=None, force=False):
        self.downloads.append(demo_id)
        Path(destination).write_bytes(b"DEMO")
        return net.DownloadResult(
            path=Path(destination),
            url=self.resolve_demo_url(demo_id),
            final_url=self.resolve_demo_url(demo_id),
            size=4,
            content_type="application/octet-stream",
        )

    def download_file(
        self, url: str, destination, *, headers=None, timeout=None, force=False
    ):
        Path(destination).write_bytes(b"DEMO")
        return net.DownloadResult(
            path=Path(destination),
            url=url,
            final_url=url,
            size=4,
            content_type="application/octet-stream",
        )

    def close(self) -> None:
        self.closed = True


class StealthTests(unittest.TestCase):
    def test_classify_page(self):
        self.assertEqual(
            classify_page(403, {}, "<title>Just a moment...</title>"), "challenge"
        )
        self.assertEqual(
            classify_page(200, {"cf-mitigated": "challenge"}, "<html>ok</html>"),
            "challenge",
        )
        self.assertEqual(
            classify_page(403, {}, "Sorry, you have been blocked"), "blocked"
        )
        self.assertEqual(classify_page(429, {}, "slow down"), "rate_limited")
        self.assertEqual(classify_page(200, {}, "<html>HLTV</html>"), "")

    def test_extract_cdn_url(self):
        text = "Final URL: https://r2-demos.hltv.org/demos/1/a-vs-b.rar?sig=secret\n"
        self.assertEqual(
            extract_cdn_url(text),
            "https://r2-demos.hltv.org/demos/1/a-vs-b.rar?sig=secret",
        )
        self.assertIsNone(extract_cdn_url("[match](https://www.hltv.org/matches/1/x)"))

    def test_cookie_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            save_state(
                path,
                [
                    {
                        "name": "cf_clearance",
                        "value": "abc",
                        "domain": ".hltv.org",
                        "path": "/",
                        "secure": True,
                    }
                ],
            )
            state = load_state(path)
            self.assertEqual(state["cookies"][0]["name"], "cf_clearance")
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cookie_header_to_cookies(self):
        cookies = cookie_header_to_cookies("cf_clearance=abc; __cf_bm=def")
        self.assertEqual(
            [cookie["name"] for cookie in cookies], ["cf_clearance", "__cf_bm"]
        )
        self.assertEqual(cookies[0]["value"], "abc")

    def test_cookie_jar_never_persists_other_sites(self):
        cookies = _dedupe_cookies(
            [
                {"name": "session", "value": "x", "domain": ".example.com"},
                {"name": "cf_clearance", "value": "y", "domain": ".hltv.org"},
                {"name": "__cf_bm", "value": "z", "domain": "r2-demos.hltv.org"},
            ]
        )
        self.assertEqual(
            sorted(cookie["name"] for cookie in cookies), ["__cf_bm", "cf_clearance"]
        )

    def test_job_for_demo_attaches_downloader_and_closer(self):
        fetcher = FakeHltvFetcher('<a href="/download/demo/113158">x</a>')
        job = hltv._job_for_demo(
            "113158",
            match_id="2399724",
            slug="a-vs-b",
            fetcher=fetcher,
            cookie="a=b",
        )
        self.assertIsNotNone(job.downloader)
        self.assertEqual(job.closer.__self__, fetcher)
        with tempfile.TemporaryDirectory() as temp:
            result = job.downloader(destination=Path(temp) / "x.rar", timeout=1)
            self.assertEqual(result.size, 4)

    def test_jobs_use_stealth_fetcher(self):
        fetcher = FakeHltvFetcher('<a href="/download/demo/113158">x</a>')
        with (
            mock.patch.object(hltv, "create_fetcher", return_value=fetcher),
            mock.patch.object(hltv.time, "sleep"),
        ):
            jobs = hltv.jobs(
                match_urls=["https://www.hltv.org/matches/2399724/a-vs-b"],
                limit=1,
                engine="auto",
                quiet=True,
            )
        self.assertEqual(len(jobs), 1)
        self.assertIsNotNone(jobs[0].downloader)
        close_jobs(jobs)
        self.assertTrue(fetcher.closed)

    def test_pipeline_uses_custom_downloader(self):
        closer = mock.Mock()

        def downloader(*, destination, timeout, force=False):
            with zipfile.ZipFile(destination, "w") as handle:
                handle.writestr("map1.dem", b"DEMO")
            return net.DownloadResult(
                path=Path(destination),
                url="https://www.hltv.org/download/demo/1",
                final_url="https://r2-demos.hltv.org/x.zip",
                size=Path(destination).stat().st_size,
                content_type="application/zip",
            )

        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "out"
            job = Job(
                source="hltv",
                demo_id="1",
                url="https://www.hltv.org/download/demo/1",
                downloader=downloader,
                closer=closer.close,
            )
            summary = run_jobs(
                [job], out_dir=out, manifest=Manifest(out), quiet=True, delay=0
            )
            self.assertEqual(summary.downloaded, 1)
            self.assertTrue((out / "hltv_1.dem").exists())
            closer.close.assert_called_once()

    def test_rar_tool_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "series.rar"
            source.write_bytes(b"RAR5")
            outside = root / "extract"
            outside.mkdir()
            seen: list[str] = []

            def run(command, **_kwargs):
                seen.append(command[0])
                if command[0].endswith("unar"):
                    return subprocess.CompletedProcess(command, 1, "", "unsupported")
                (outside / "a.dem").write_bytes(b"DEMO")
                return subprocess.CompletedProcess(command, 0, "", "")

            def which(name):
                return f"/usr/bin/{name}" if name in {"unar", "bsdtar"} else None

            with (
                mock.patch.object(archive.shutil, "which", side_effect=which),
                mock.patch.object(archive.subprocess, "run", side_effect=run),
            ):
                found = archive._extract_with_external(source, outside)
            self.assertEqual([path.name for path in found], ["a.dem"])
            self.assertEqual([Path(name).name for name in seen], ["unar", "bsdtar"])


class _RangeHandler(BaseHTTPRequestHandler):
    payload = b"CS2-DEMO-" * 120_000

    def do_GET(self):
        range_header = self.headers.get("Range") or ""
        if range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            body = self.payload[start:]
            self.send_response(206)
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}",
            )
        else:
            body = self.payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(
    importlib.util.find_spec("curl_cffi") is not None, "curl_cffi not installed"
)
class DownloadResumeTests(unittest.TestCase):
    def test_range_resume(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                destination = root / "demo.rar"
                fetcher = HltvFetcher(
                    StealthConfig(
                        engine="curl",
                        quiet=True,
                        state_file=root / "state.json",
                    )
                )
                url = f"http://127.0.0.1:{server.server_port}/demo.rar"
                try:
                    result = fetcher.download_file(url, destination)
                    self.assertEqual(result.size, len(_RangeHandler.payload))
                    self.assertEqual(destination.read_bytes(), _RangeHandler.payload)

                    part = destination.with_name(destination.name + ".part")
                    part.write_bytes(_RangeHandler.payload[:100_000])
                    destination.unlink()
                    result = fetcher.download_file(url, destination)
                    self.assertEqual(result.size, len(_RangeHandler.payload))
                    self.assertEqual(destination.read_bytes(), _RangeHandler.payload)
                finally:
                    fetcher.close()
        finally:
            server.shutdown()
            server.server_close()


class _ChallengeHandler(BaseHTTPRequestHandler):
    payload = b"FAKE-DEMO-ARCHIVE" * 64

    def do_GET(self):
        if self.path.startswith("/file"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="x.rar"')
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
            return
        body = (
            b"<html><head><title>Just a moment...</title></head><body>"
            b"<p>Checking your browser</p>"
            b"<input type=checkbox onclick=\"window.location='/file'\">"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(
    os.environ.get("RUN_BROWSER_TESTS") == "1"
    and (
        importlib.util.find_spec("playwright") is not None
        or importlib.util.find_spec("patchright") is not None
    ),
    "set RUN_BROWSER_TESTS=1 and install playwright/patchright",
)
class BrowserDownloadTests(unittest.TestCase):
    def test_challenge_page_then_download(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ChallengeHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                session = BrowserSession(
                    StealthConfig(
                        engine="browser",
                        headless=True,
                        auto_headed=False,
                        challenge_timeout=15,
                        state_file=root / "state.json",
                    )
                )
                session.start()
                try:
                    destination = root / "out.rar"
                    result = session.download(
                        f"http://127.0.0.1:{server.server_port}/page",
                        destination,
                        timeout=20,
                    )
                    self.assertEqual(result.size, len(_ChallengeHandler.payload))
                    self.assertEqual(
                        destination.read_bytes(), _ChallengeHandler.payload
                    )
                finally:
                    session.close()
        finally:
            server.shutdown()
            server.server_close()


class StoreTests(unittest.TestCase):
    def test_zip_multi_extract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "series.zip"
            with zipfile.ZipFile(archive_path, "w") as handle:
                handle.writestr("a.dem", b"DEMO-A")
                handle.writestr("maps/b.dem", b"DEMO-B")
            demos = extract_demos(archive_path, root, "hltv_1")
            self.assertEqual(len(demos), 2)
            self.assertEqual(
                {demo.path.name for demo in demos},
                {"hltv_1__a.dem", "hltv_1__maps_b.dem"},
            )
            self.assertFalse(archive_path.exists())
            self.assertEqual(demos[0].size, 6)

    def test_tar_xz_extract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "series.tar.xz"
            source = root / "a.dem"
            source.write_bytes(b"DEMO")
            with tarfile.open(archive_path, "w:xz") as handle:
                handle.add(source, arcname="a.dem")
            source.unlink()
            demos = extract_demos(archive_path, root, "series")
            self.assertEqual(len(demos), 1)
            self.assertEqual(demos[0].path.name, "series.dem")
            self.assertEqual(demos[0].path.read_bytes(), b"DEMO")

    def test_tar_zst_extract(self):
        if not shutil.which("zstd"):
            self.skipTest("zstd 未安装")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tar_path = root / "series.tar"
            source = root / "a.dem"
            source.write_bytes(b"DEMO")
            with tarfile.open(tar_path, "w") as handle:
                handle.add(source, arcname="a.dem")
            source.unlink()
            archive_path = root / "series.tar.zst"
            subprocess.run(
                ["zstd", "-q", "-f", "-o", str(archive_path), str(tar_path)],
                check=True,
            )
            demos = extract_demos(archive_path, root, "series")
            self.assertEqual(len(demos), 1)
            self.assertEqual(demos[0].path.read_bytes(), b"DEMO")

    def test_zip_duplicate_basenames(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "series.zip"
            with zipfile.ZipFile(archive_path, "w") as handle:
                handle.writestr("map1/a.dem", b"ONE")
                handle.writestr("map2/a.dem", b"TWO")
            demos = extract_demos(archive_path, root, "series")
            self.assertEqual(len(demos), 2)
            self.assertEqual(len({demo.path.name for demo in demos}), 2)
            self.assertEqual(
                {demo.path.read_bytes() for demo in demos}, {b"ONE", b"TWO"}
            )

    def test_gz_extract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive_path = root / "match.dem.gz"
            with gzip.open(archive_path, "wb") as handle:
                handle.write(b"DEMO")
            demos = extract_demos(archive_path, root, "hltv_1")
            self.assertEqual(len(demos), 1)
            self.assertEqual(demos[0].path.name, "hltv_1.dem")
            self.assertEqual(demos[0].path.read_bytes(), b"DEMO")

    def test_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = Manifest(root)
            digest = "a" * 64
            manifest.add(
                {
                    "source": "hltv",
                    "match_id": "1",
                    "demo_id": "",
                    "path": "x.dem",
                    "bytes": 10,
                    "sha256": digest,
                }
            )
            self.assertTrue(manifest.has_key(demo_key("hltv", "1", "")))
            self.assertTrue(manifest.has_hash(digest))
            reloaded = Manifest(root)
            self.assertEqual(reloaded.summary()["demos"], 1)
            self.assertTrue((root / "manifest.jsonl").exists())

    def test_local_jobs_and_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "my_demo.dem"
            source.write_bytes(b"DEMO")
            out = root / "out"
            jobs = local_jobs([source])
            self.assertEqual(len(jobs), 1)
            manifest = Manifest(out)
            summary = run_jobs(
                jobs, out_dir=out, manifest=manifest, quiet=True, delay=0
            )
            self.assertEqual(summary.downloaded, 1)
            self.assertEqual(summary.failed, 0)
            self.assertTrue((out / "import_my_demo.dem").exists())
            manifest2 = Manifest(out)
            summary2 = run_jobs(
                local_jobs([source]),
                out_dir=out,
                manifest=manifest2,
                quiet=True,
                delay=0,
            )
            self.assertEqual(summary2.downloaded, 0)
            self.assertGreaterEqual(summary2.skipped, 1)


class CliTests(unittest.TestCase):
    def test_dry_run_demo_id(self):
        job = Job(
            source="hltv",
            demo_id="113158",
            url="https://www.hltv.org/download/demo/113158",
        )
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "out"
            buffer = io.StringIO()
            with (
                mock.patch.object(hltv, "jobs", return_value=[job]),
                contextlib.redirect_stdout(buffer),
            ):
                code = cli.main(["--dry-run", "--demo-id", "113158", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("计划下载 1", buffer.getvalue())
            self.assertTrue(out.exists())

    def test_fail_fast_returns_clean_error(self):
        job = Job(
            source="hltv",
            demo_id="113158",
            url="https://www.hltv.org/download/demo/113158",
        )
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "out"
            stderr = io.StringIO()
            with (
                mock.patch.object(hltv, "jobs", return_value=[job]),
                mock.patch.object(cli, "run_jobs", side_effect=RuntimeError("boom")),
                contextlib.redirect_stderr(stderr),
            ):
                code = cli.main(
                    ["--fail-fast", "--demo-id", "113158", "--out", str(out)]
                )
            self.assertEqual(code, 1)
            self.assertIn("错误: boom", stderr.getvalue())

    def test_json_zero_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "out"
            buffer = io.StringIO()
            with (
                mock.patch.object(hltv, "jobs", return_value=[]),
                contextlib.redirect_stdout(buffer),
            ):
                code = cli.main(["--json", "--demo-id", "113158", "--out", str(out)])
            self.assertEqual(code, 0)
            payload = json.loads(buffer.getvalue())
            self.assertEqual(payload["planned"], 0)
            self.assertIn("message", payload)

    def test_cdp_requires_url(self):
        with self.assertRaises(SystemExit):
            cli.main(["--engine", "cdp", "--demo-id", "113158"])


if __name__ == "__main__":
    unittest.main()
