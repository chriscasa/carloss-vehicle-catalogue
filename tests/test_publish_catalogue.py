import base64
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import publish_catalogue as publisher
from catalogue_validation import REQUIRED_HEADERS


def document():
    return {"source": "ASTRA / FEDRO", "sourceURL": "https://www.astra.admin.ch/en/vehicle-data", "updatedAt": "2026-11-04",
            "vehicles": [{"id": f"astra-{index:016x}", "make": "Make", "model": f"Model {index}", "energy": "Petrol"}
                         for index in range(10_000)]}


class Response(io.BytesIO):
    status = 200

    def __init__(self, body, headers, url):
        super().__init__(body)
        self.headers = headers
        self.url = url

    def geturl(self):
        return self.url


class OfficialHTTP:
    def __init__(self):
        self.requests = []
        self.rows = {name: (";".join(sorted(REQUIRED_HEADERS[name])) + "\n").encode() for name in publisher.SOURCE_URLS}
        self.tags = {name: '"v1"' for name in publisher.SOURCE_URLS}

    def info(self, name):
        return publisher._source_info(name, self.headers(name))

    def headers(self, name):
        return {"Content-Length": str(len(self.rows[name])), "Last-Modified": "Wed, 04 Nov 2026 06:59:19 GMT", "ETag": self.tags[name]}

    def __call__(self, request, timeout):
        self.requests.append(request)
        name = next(name for name, url in publisher.SOURCE_URLS.items() if request.full_url == url)
        body = b"" if request.get_method() == "HEAD" else self.rows[name]
        return Response(body, self.headers(name), request.full_url)


class PublisherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        private = Path(cls.keys.name) / "private.pem"
        cls.public_key = Path(cls.keys.name) / "catalogue-v1.pub"
        subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-out", str(private)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.chmod(private, 0o600)
        cls.private_pem = private.read_text()
        subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(cls.public_key)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "v1"
        self.cache = self.root / "raw-cache"
        self.normalizer = self.root / "normalizer.py"
        self.normalizer.write_text("# reviewed normalizer\n")
        self.http = OfficialHTTP()

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, value=None, sources=None, pem=None, published_at="2026-11-17T03:17:00Z"):
        return publisher.publish_document(value or document(), self.output, sources or [], publisher.digest_file(self.normalizer),
            private_pem=pem or self.private_pem, public_key=self.public_key, published_at=published_at)

    def test_deterministic_gzip_hash_and_openssl_signed_manifest_round_trip(self):
        source = document()
        payload = self.publish(source)
        restored, verified = publisher.verify_manifest(self.output, self.public_key)
        self.assertEqual(restored, source)
        self.assertEqual(verified, payload)
        compressed = (self.output / payload["file"]).read_bytes()
        self.assertEqual(compressed, publisher.deterministic_gzip(publisher.canonical(source)))
        self.assertEqual(compressed[4:8], b"\x00\x00\x00\x00")
        self.assertEqual(hashlib.sha256(compressed).hexdigest(), payload["sha256"])
        self.assertEqual(len(compressed), payload["compressedBytes"])
        self.assertEqual(len(gzip.decompress(compressed)), payload["uncompressedBytes"])
        wrapper = publisher.read_json(self.output / "manifest.json")
        self.assertEqual(wrapper["keyID"], "catalogue-v1")
        self.assertEqual(base64.b64decode(wrapper["signature"])[0], 0x30, "Signature must use ASN.1 DER encoding.")

    def test_signed_payload_tampering_is_rejected(self):
        self.publish()
        wrapper = publisher.read_json(self.output / "manifest.json")
        payload = publisher.unpack_manifest(wrapper)
        payload["publishedAt"] = "2026-11-17T04:17:00Z"
        wrapper["payload"] = base64.b64encode(publisher.canonical(payload)).decode()
        publisher.atomic_write(self.output / "manifest.json", publisher.canonical(wrapper))
        with self.assertRaises(ValueError):
            publisher.verify_manifest(self.output, self.public_key)

    def test_payload_hash_corruption_is_rejected(self):
        payload = self.publish()
        path = self.output / payload["file"]
        compressed = bytearray(path.read_bytes())
        compressed[-1] ^= 1
        path.write_bytes(compressed)
        with self.assertRaises(ValueError):
            publisher.verify_manifest(self.output, self.public_key)

    def test_schema_failure_retains_last_good_manifest_and_payload(self):
        payload = self.publish()
        original = (self.output / "manifest.json").read_bytes()
        changed = document()
        changed["vehicles"][0]["VIN"] = "must never be published"
        with self.assertRaises(ValueError):
            self.publish(changed)
        self.assertEqual((self.output / "manifest.json").read_bytes(), original)
        self.assertTrue((self.output / payload["file"]).exists())

    def test_wrong_signing_secret_cannot_replace_a_pinned_publication(self):
        self.publish()
        original = (self.output / "manifest.json").read_bytes()
        wrong = self.root / "wrong-private.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-out", str(wrong)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        changed = document()
        changed["vehicles"][0]["model"] = "Changed"
        with self.assertRaises(ValueError):
            self.publish(changed, pem=wrong.read_text())
        self.assertEqual((self.output / "manifest.json").read_bytes(), original)

    def test_source_date_mismatch_cannot_publish_a_seed(self):
        observations = [self.http.info(name) for name in publisher.SOURCE_URLS]
        changed = document()
        changed["updatedAt"] = "2026-11-03"
        with self.assertRaises(ValueError):
            self.publish(changed, sources=observations)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_unchanged_sources_only_issue_three_head_requests(self):
        self.publish(sources=[self.http.info(name) for name in publisher.SOURCE_URLS])
        plan = publisher.check_sources(self.output, self.normalizer, opener=self.http)
        self.assertFalse(plan["needsBuild"])
        self.assertEqual(len(self.http.requests), 3)
        self.assertTrue(all(request.get_method() == "HEAD" for request in self.http.requests))
        self.assertEqual(publisher.source_date(plan["sources"]), "2026-11-04")

    def test_changed_etag_or_normalizer_requires_a_rebuild(self):
        self.publish(sources=[self.http.info(name) for name in publisher.SOURCE_URLS])
        self.http.tags["edata"] = '"v2"'
        changed = publisher.check_sources(self.output, self.normalizer, opener=self.http)
        self.assertTrue(changed["needsBuild"])
        self.http.tags["edata"] = '"v1"'
        self.normalizer.write_text("# reviewed normalizer version 2\n")
        changed = publisher.check_sources(self.output, self.normalizer, opener=self.http)
        self.assertTrue(changed["needsBuild"])

    def test_warm_input_cache_reuses_unchanged_downloads_and_pins_headers(self):
        source = self.http.info("autos")
        path, metadata = publisher.download_source(source, self.cache, opener=self.http)
        first_bytes = path.read_bytes()
        _, reused = publisher.download_source(source, self.cache, metadata["header"], opener=self.http)
        self.assertEqual(metadata, reused)
        self.assertEqual(len(self.http.requests), 1)
        self.http.rows["autos"] = b"newUnknownHeader\n"
        self.http.tags["autos"] = '"v2"'
        with self.assertRaises(ValueError):
            publisher.download_source(self.http.info("autos"), self.cache, metadata["header"], opener=self.http)
        self.assertEqual(path.read_bytes(), first_bytes)

    def test_source_changed_after_head_cannot_enter_cache(self):
        source = self.http.info("autos")
        self.http.tags["autos"] = '"v2"'
        with self.assertRaises(ValueError):
            publisher.download_source(source, self.cache, opener=self.http)
        self.assertFalse((self.cache / "autos.csv").exists())

    def test_aggregate_download_and_manifest_path_bounds(self):
        sources = [self.http.info(name) for name in publisher.SOURCE_URLS]
        for source in sources:
            source["contentLength"] = publisher.RAW_LIMIT
        with self.assertRaises(ValueError):
            publisher.validate_sources(sources)
        self.publish()
        wrapper = publisher.read_json(self.output / "manifest.json")
        payload = publisher.unpack_manifest(wrapper)
        payload["file"] = "../outside.json.gz"
        wrapper["payload"] = base64.b64encode(publisher.canonical(payload)).decode()
        with self.assertRaises(ValueError):
            publisher.unpack_manifest(wrapper)

    def test_keeps_current_and_two_previous_payloads(self):
        revisions = []
        for index in range(4):
            value = document()
            value["vehicles"][0]["model"] = f"Variant {index}"
            revisions.append(self.publish(value, published_at=f"2026-11-17T03:17:{index:02d}Z")["revision"])
        retained = {path.name.removesuffix(".json.gz") for path in (self.output / "catalogue").glob("*.json.gz")}
        self.assertEqual(retained, set(revisions[-3:]))
        self.assertEqual(publisher.verify_manifest(self.output, self.public_key)[1]["revision"], revisions[-1])

    def test_changed_revision_in_same_second_advances_the_native_publication_floor(self):
        first = self.publish()
        changed = document()
        changed["vehicles"][0]["model"] = "Changed variant"
        second = self.publish(changed)
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertEqual(first["publishedAt"], "2026-11-17T03:17:00Z")
        self.assertEqual(second["publishedAt"], "2026-11-17T03:17:01Z")
        self.assertGreater(datetime.fromisoformat(second["publishedAt"].replace("Z", "+00:00")),
                           datetime.fromisoformat(first["publishedAt"].replace("Z", "+00:00")))
        self.assertEqual(publisher.verify_manifest(self.output, self.public_key)[1], second)

    def test_regressed_publication_clock_cannot_mutate_the_previous_publication(self):
        self.publish()
        before = {str(path.relative_to(self.output)): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        changed = document()
        changed["vehicles"][0]["model"] = "Changed variant"
        with self.assertRaisesRegex(ValueError, "clock regressed"):
            self.publish(changed, published_at="2026-11-17T03:16:59Z")
        after = {str(path.relative_to(self.output)): path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_unchanged_content_retains_its_signature_and_date_even_if_the_clock_regresses(self):
        first = self.publish()
        before = (self.output / "manifest.json").read_bytes()
        second = self.publish(published_at="2026-11-17T03:16:59Z")
        self.assertEqual(second, first)
        self.assertEqual((self.output / "manifest.json").read_bytes(), before)

    def test_same_second_adjustment_cannot_exceed_native_future_clock_tolerance(self):
        previous = "2026-11-17T03:17:00Z"
        instant = datetime(2026, 11, 17, 3, 17, tzinfo=timezone.utc)
        self.assertEqual(publisher.next_publication_time(previous, previous, now=instant), "2026-11-17T03:17:01Z")
        with self.assertRaisesRegex(ValueError, "clock tolerance"):
            publisher.next_publication_time(previous, previous, now=instant - timedelta(seconds=300))

    def test_full_pipeline_downloads_only_changed_inputs_and_excludes_signing_env_from_normalizer(self):
        seed = self.root / "seed.json"
        seed.write_bytes(publisher.canonical(document()))
        self.normalizer.write_text(
            "import argparse, os, pathlib\n"
            "assert 'CATALOGUE_SIGNING_PRIVATE_KEY' not in os.environ\n"
            "p=argparse.ArgumentParser()\n"
            "for name in ['autos','emissions','edata','updated-at','output']: p.add_argument('--'+name)\n"
            "args=p.parse_args()\n"
            f"pathlib.Path(args.output).write_bytes(pathlib.Path({str(seed)!r}).read_bytes())\n")
        plan = publisher.check_sources(self.output, self.normalizer, opener=self.http)
        with patch.dict(os.environ, {publisher.SIGNING_ENV: self.private_pem}):
            publisher.publish_from_sources(plan, self.output, self.cache, self.normalizer, self.public_key, opener=self.http)
        self.assertEqual(sum(request.get_method() == "GET" for request in self.http.requests), 3)
        self.http.requests.clear()
        self.http.tags["emissions"] = '"v2"'
        plan = publisher.check_sources(self.output, self.normalizer, opener=self.http)
        with patch.dict(os.environ, {publisher.SIGNING_ENV: self.private_pem}):
            publisher.publish_from_sources(plan, self.output, self.cache, self.normalizer, self.public_key, opener=self.http)
        gets = [request.full_url for request in self.http.requests if request.get_method() == "GET"]
        self.assertEqual(gets, [publisher.SOURCE_URLS["emissions"]])
        self.assertEqual(publisher.verify_manifest(self.output, self.public_key)[1]["sourceUpdatedAt"], "2026-11-04")


if __name__ == "__main__":
    unittest.main()
