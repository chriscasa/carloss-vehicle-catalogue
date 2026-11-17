"""Bounded, local HTTP fixtures for official-source range downloads."""
from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import publish_catalogue as publisher
from catalogue_validation import REQUIRED_HEADERS


class RangeResponse(io.BytesIO):
    def __init__(self, server, start, end, body, headers, status=206, url=None):
        super().__init__(body)
        self.server = server
        self.start = start
        self.end = end
        self.headers = headers
        self.status = status
        self.url = url or server.source["url"]
        self.read_calls = 0
        self.first_read = True
        self.exited = False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        self.read_calls += 1
        if self.first_read and self.server.gated and self.start < 4 * self.server.chunk_bytes:
            self.first_read = False
            with self.server.lock:
                self.server.waiting_readers += 1
                if self.server.waiting_readers >= 4:
                    self.server.readers_ready.set()
            # A broken sequential implementation fails assertions instead of
            # hanging the test runner indefinitely.
            self.server.readers_ready.wait(2)
            if self.start == 0:
                self.server.nonfirst_completed.wait(2)
        return super().read(size)

    def read1(self, size=-1):
        # Keep concurrency/read accounting visible when the downloader uses
        # HTTPResponse's single-read API instead of accumulating read(n).
        return self.read(size)

    def __exit__(self, exc_type, exc_value, traceback):
        with self.server.lock:
            if not self.exited:
                self.exited = True
                self.server.active -= 1
                if exc_type is None:
                    self.server.completed.append(self.start)
                    if self.start != 0:
                        self.server.nonfirst_completed.set()
        return super().__exit__(exc_type, exc_value, traceback)


class RangeHTTP:
    """Requests observe the same complete source identity as its HEAD metadata."""
    chunk_bytes = 128

    def __init__(self, *, failure=None, fail_attempts=0, malformed=None, gated=False, etag='"v1"'):
        self.header = sorted(REQUIRED_HEADERS["autos"])
        self.payload = (";".join(self.header) + "\n").encode("utf-8") + b"synthetic-public-row;" * 32 + b"\n"
        self.source = {"name": "autos", "url": publisher.SOURCE_URLS["autos"], "etag": etag,
                       "lastModified": "Wed, 04 Nov 2026 06:59:19 GMT", "contentLength": len(self.payload)}
        self.failure = failure
        self.fail_attempts = fail_attempts
        self.malformed = malformed
        self.gated = gated
        self.lock = threading.Lock()
        self.readers_ready = threading.Event()
        self.nonfirst_completed = threading.Event()
        self.waiting_readers = 0
        self.calls = []
        self.attempts = Counter()
        self.responses = []
        self.completed = []
        self.active = 0
        self.maximum_active = 0

    def __call__(self, request, timeout):
        if not 0 < timeout <= 30:
            raise AssertionError("Every range request needs a bounded transport timeout")
        if request.get_method() != "GET":
            raise AssertionError("The downloader receives validated HEAD metadata; it must issue GETs")
        headers = {key.lower(): value for key, value in request.header_items()}
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", headers.get("range", ""))
        if match is None:
            raise AssertionError("Large inputs must use bounded byte ranges")
        start, end = map(int, match.groups())
        if not (0 <= start <= end < len(self.payload)) or end - start + 1 > self.chunk_bytes:
            raise AssertionError("A worker requested an invalid or unbounded byte range")
        if headers.get("if-unmodified-since") != self.source["lastModified"]:
            raise AssertionError("Ranges must remain bound to the source's modification time")
        expected_tag = None if self.source["etag"].startswith("W/") else self.source["etag"]
        if headers.get("if-match") != expected_tag:
            raise AssertionError("If-Match must bind strong ETags and must not send weak ETags")
        if headers.get("accept-encoding") != "identity":
            raise AssertionError("Byte offsets require an unencoded representation")
        with self.lock:
            self.calls.append((start, end))
            self.attempts[start] += 1
            attempt = self.attempts[start]
        if start == 0 and attempt <= self.fail_attempts:
            if self.failure == "network":
                raise URLError("temporary transport failure")
            if self.failure == "http503":
                raise HTTPError(request.full_url, 503, "temporarily unavailable", {}, io.BytesIO())
        body = self.payload[start:end + 1]
        if start == 0 and self.failure == "short" and attempt <= self.fail_attempts:
            body = body[:-1]
        response_headers = {"Content-Range": f"bytes {start}-{end}/{len(self.payload)}",
            "Content-Length": str(end - start + 1), "Last-Modified": self.source["lastModified"], "ETag": self.source["etag"]}
        status, response_url = 206, None
        if start == 0:
            if self.malformed == "range":
                response_headers["Content-Range"] = f"bytes {start + 1}-{end + 1}/{len(self.payload)}"
            elif self.malformed == "total":
                response_headers["Content-Range"] = f"bytes {start}-{end}/{len(self.payload) + 1}"
            elif self.malformed == "missing-range":
                del response_headers["Content-Range"]
            elif self.malformed == "length":
                response_headers["Content-Length"] = str(end - start + 2)
            elif self.malformed == "etag":
                response_headers["ETag"] = '"changed-after-HEAD"'
            elif self.malformed == "missing-etag":
                del response_headers["ETag"]
            elif self.malformed == "modified":
                response_headers["Last-Modified"] = "Thu, 05 Nov 2026 06:59:19 GMT"
            elif self.malformed == "ignored-range":
                status = 200
                response_headers["Content-Length"] = str(len(self.payload))
                body = self.payload
            elif self.malformed == "redirect":
                response_url = "https://unexpected.example/source.csv"
        response = RangeResponse(self, start, end, body, response_headers, status, response_url)
        with self.lock:
            self.responses.append(response)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        return response


class RangeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name)
        settings = patch.multiple(publisher, RANGE_THRESHOLD_BYTES=64, RANGE_CHUNK_BYTES=128,
                                  RANGE_WORKERS=4, DOWNLOAD_ATTEMPTS=3, RETRY_BACKOFF_SECONDS=0)
        settings.start()
        self.addCleanup(settings.stop)

    def download(self, server, previous_header=None):
        return publisher.download_source(server.source, self.cache, previous_header=previous_header, opener=server)

    def assert_clean(self):
        self.assertEqual(list(self.cache.glob(".astra*")), [], "Failed downloads must remove temporary range files")

    def test_reconstructed_ranges_preserve_exact_bytes_digest_and_header(self):
        server = RangeHTTP()
        path, metadata = self.download(server, server.header)
        self.assertEqual(path.read_bytes(), server.payload)
        self.assertEqual(metadata["downloadSHA256"], hashlib.sha256(server.payload).hexdigest())
        self.assertEqual(metadata["header"], server.header)
        self.assertEqual(metadata["headerSHA256"], hashlib.sha256(publisher.canonical(server.header)).hexdigest())
        self.assertEqual(metadata["contentLength"], len(server.payload), "Published provenance records the full source size")
        expected = [(offset, min(offset + 127, len(server.payload) - 1)) for offset in range(0, len(server.payload), 128)]
        self.assertEqual(sorted(server.calls), expected)
        self.assertLessEqual(server.maximum_active, 4)
        self.assert_clean()

    def test_out_of_order_chunks_use_four_bounded_workers_and_write_at_correct_offsets(self):
        server = RangeHTTP(gated=True)
        path, _ = self.download(server)
        self.assertEqual(path.read_bytes(), server.payload)
        self.assertEqual(server.maximum_active, 4)
        self.assertGreaterEqual(server.waiting_readers, 4)
        self.assertNotEqual(server.completed[0], 0, "Completion order must not determine file order")
        self.assert_clean()

    def test_verified_warm_cache_performs_no_gets(self):
        server = RangeHTTP()
        first_path, first_metadata = self.download(server)
        count = len(server.calls)
        path, metadata = self.download(server, server.header)
        self.assertEqual((path, metadata), (first_path, first_metadata))
        self.assertEqual(len(server.calls), count)
        self.assertEqual(path.read_bytes(), server.payload)

    def test_short_chunk_is_retried_without_restarting_unchanged_chunks(self):
        server = RangeHTTP(failure="short", fail_attempts=2)
        path, _ = self.download(server)
        self.assertEqual(path.read_bytes(), server.payload)
        self.assertEqual(server.attempts[0], 3)
        self.assertTrue(all(count == 1 for start, count in server.attempts.items() if start != 0))
        self.assert_clean()

    def test_transient_transport_and_http_503_retry_the_same_range(self):
        for failure in ("network", "http503"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                server = RangeHTTP(failure=failure, fail_attempts=1)
                path, _ = publisher.download_source(server.source, Path(directory), opener=server)
                self.assertEqual(path.read_bytes(), server.payload)
                self.assertEqual(server.attempts[0], 2)
                self.assertTrue(all(count == 1 for start, count in server.attempts.items() if start != 0))

    def test_invalid_range_or_source_identity_is_not_retried_or_read(self):
        for problem in ("range", "total", "missing-range", "length", "etag", "missing-etag", "modified", "redirect"):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as directory:
                server = RangeHTTP(malformed=problem)
                with self.assertRaises(ValueError):
                    publisher.download_source(server.source, Path(directory), opener=server)
                self.assertEqual(server.attempts[0], 1)
                broken = [response for response in server.responses if response.start == 0]
                self.assertEqual(len(broken), 1)
                self.assertEqual(broken[0].read_calls, 0, "Reject inconsistent range metadata before reading its body")
                self.assertEqual(list(Path(directory).glob(".astra*")), [])
                self.assertFalse((Path(directory) / "autos.csv").exists())

    def test_ignored_range_full_response_is_rejected_before_large_body_is_read(self):
        server = RangeHTTP(malformed="ignored-range")
        with self.assertRaises(ValueError):
            self.download(server)
        self.assertEqual(server.attempts[0], 1)
        self.assertEqual(next(response for response in server.responses if response.start == 0).read_calls, 0)
        self.assertFalse((self.cache / "autos.csv").exists())
        self.assert_clean()

    def test_permanent_short_read_stops_after_three_attempts_and_keeps_last_good_cache(self):
        server = RangeHTTP(failure="short", fail_attempts=10)
        old_body = b"last verified source remains intact\n"
        old_source = {**server.source, "etag": '"old"', "contentLength": len(old_body)}
        old_metadata = {**old_source, "downloadSHA256": hashlib.sha256(old_body).hexdigest()}
        path = self.cache / "autos.csv"
        metadata_path = self.cache / "autos.metadata.json"
        path.write_bytes(old_body)
        old_metadata_bytes = (json.dumps(old_metadata) + "\n").encode()
        metadata_path.write_bytes(old_metadata_bytes)
        with self.assertRaises((ValueError, OSError)):
            self.download(server)
        self.assertEqual(server.attempts[0], 3)
        self.assertEqual(path.read_bytes(), old_body)
        self.assertEqual(metadata_path.read_bytes(), old_metadata_bytes)
        self.assert_clean()

    def test_weak_etag_uses_modification_time_without_invalid_if_match(self):
        server = RangeHTTP(etag='W/"v1"')
        path, metadata = self.download(server)
        self.assertEqual(path.read_bytes(), server.payload)
        self.assertEqual(metadata["etag"], 'W/"v1"')

    def test_header_drift_does_not_replace_existing_source(self):
        server = RangeHTTP()
        path = self.cache / "autos.csv"
        path.write_bytes(b"old source\n")
        with self.assertRaises(ValueError):
            self.download(server, server.header + ["retired-source-column"])
        self.assertEqual(path.read_bytes(), b"old source\n")
        self.assertFalse((self.cache / "autos.metadata.json").exists())
        self.assert_clean()

    def test_expired_source_deadline_never_opens_a_transfer_and_cleans_temp_files(self):
        server = RangeHTTP()
        with patch.object(publisher, "SOURCE_DEADLINE_SECONDS", 0):
            with self.assertRaises(ValueError):
                self.download(server)
        self.assertEqual(server.calls, [])
        self.assertFalse((self.cache / "autos.csv").exists())
        self.assert_clean()

    def test_single_socket_reads_recheck_deadline_instead_of_accumulating_a_slow_trickle(self):
        class Clock:
            now = 100.0

        class Socket:
            timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

        clock = Clock()
        connection = Socket()

        class TrickleResponse:
            def __init__(self):
                self.fp = type("Buffered", (), {"raw": type("Raw", (), {"_sock": connection})()})()
                self.single_reads = 0

            def read(self, size):
                raise AssertionError("Accumulating read(n) cannot enforce an overall deadline during a slow trickle")

            def read1(self, size):
                self.single_reads += 1
                clock.now += 0.6
                return b"x"

        response = TrickleResponse()
        with patch.object(publisher.time, "monotonic", side_effect=lambda: clock.now):
            self.assertEqual(publisher._read_transfer_piece(response, 128, 101.0), b"x")
            with self.assertRaisesRegex(ValueError, "deadline"):
                publisher._read_transfer_piece(response, 128, 101.0)
        self.assertEqual(response.single_reads, 2)
        self.assertAlmostEqual(connection.timeouts[0], 1.0)
        self.assertAlmostEqual(connection.timeouts[1], 0.4)


if __name__ == "__main__":
    unittest.main()
