#!/usr/bin/env python3
"""Build and sign the public, data-only Carloss vehicle catalogue feed."""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from catalogue_validation import validate_catalogue, validate_header

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URLS = {
    "autos": "https://opendata.astra.admin.ch/ivzod/4000-Typengenehmigungen_TAS/4300-Datensaetze/TAS_Automobil.csv",
    "emissions": "https://opendata.astra.admin.ch/ivzod/4000-Typengenehmigungen_TAS/4300-Datensaetze/TAS_Emission.csv",
    "edata": "https://opendata.astra.admin.ch/ivzod/3000-eDatenblatt/eDatenblatt.csv",
}
RAW_LIMIT = 2 * 1024 * 1024 * 1024
JSON_LIMIT = 32 * 1024 * 1024
GZIP_LIMIT = 4 * 1024 * 1024
SMALL_JSON_LIMIT = 1024 * 1024
KEY_ID = "catalogue-v1"
SIGNING_ENV = "CATALOGUE_SIGNING_PRIVATE_KEY"
SHA = re.compile(r"[0-9a-f]{64}\Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def decode_json(data):
    return json.loads(data, object_pairs_hook=_unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def read_json(path, limit=SMALL_JSON_LIMIT):
    with Path(path).open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("JSON file exceeds the size limit")
    return decode_json(data)


def digest_file(path, limit=RAW_LIMIT):
    digest, size = hashlib.sha256(), 0
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise ValueError("File exceeds its size limit")
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".publish-", delete=False) as handle:
            name = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name and name.exists():
            name.unlink()


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def next_publication_time(candidate, previous=None, now=None):
    """Respect the client's rollback floor without masking a regressed clock."""
    def parse(value):
        if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
            raise ValueError("Publication time must be a UTC timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Publication time must be a UTC timestamp")
        return parsed

    value = parse(candidate)
    if previous is not None:
        floor = parse(previous)
        if value < floor:
            raise ValueError("Publication clock regressed; the previous manifest is retained")
        if value == floor:
            # Two changed revisions can finish in the same clock second.
            value = floor + timedelta(seconds=1)
    if value > (now or datetime.now(timezone.utc)) + timedelta(seconds=300):
        raise ValueError("Publication time exceeds the client's clock tolerance")
    precision = "microseconds" if value.microsecond else "seconds"
    return value.astimezone(timezone.utc).isoformat(timespec=precision).replace("+00:00", "Z")


def _source_info(name, headers):
    try:
        size = int(headers.get("Content-Length", ""))
        modified = parsedate_to_datetime(headers.get("Last-Modified", ""))
        if modified.tzinfo is None or not 0 < size <= RAW_LIMIT:
            raise ValueError()
        modified = modified.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError, AttributeError):
        raise ValueError(f"Invalid source size or modification date: {name}") from None
    etag = headers.get("ETag")
    if etag is not None and (len(etag) > 1024 or "\n" in etag or "\r" in etag):
        raise ValueError("Invalid source ETag")
    return {"name": name, "url": SOURCE_URLS[name], "etag": etag,
            "lastModified": format_datetime(modified, usegmt=True), "contentLength": size}


def source_identity(source):
    return {key: source.get(key) for key in ("name", "url", "etag", "lastModified", "contentLength")}


def validate_sources(sources):
    if not isinstance(sources, list) or len(sources) != len(SOURCE_URLS):
        raise ValueError("Exactly three official source observations are required")
    checked = {}
    for source in sources:
        if not isinstance(source, dict) or source.get("name") not in SOURCE_URLS:
            raise ValueError("Unknown source")
        allowed = {"name", "url", "etag", "lastModified", "contentLength", "downloadSHA256", "header", "headerSHA256"}
        if set(source) - allowed:
            raise ValueError("Unexpected source metadata fields")
        name = source["name"]
        if name in checked or source.get("url") != SOURCE_URLS[name]:
            raise ValueError("Duplicate or unofficial source")
        info = _source_info(name, {"ETag": source.get("etag"), "Last-Modified": source.get("lastModified"),
                                   "Content-Length": str(source.get("contentLength", ""))})
        validated = {**source, **info}
        if "header" in source:
            validate_header(name, source["header"])
            header_sha = hashlib.sha256(canonical(source["header"])).hexdigest()
            if source.get("headerSHA256", header_sha) != header_sha:
                raise ValueError("Source header digest mismatch")
            validated["headerSHA256"] = header_sha
        elif "headerSHA256" in source:
            raise ValueError("Source header digest has no header")
        if "downloadSHA256" in source and (not isinstance(source["downloadSHA256"], str) or not SHA.fullmatch(source["downloadSHA256"])):
            raise ValueError("Invalid source download digest")
        checked[name] = validated
    if sum(item["contentLength"] for item in checked.values()) > RAW_LIMIT:
        raise ValueError("Aggregate official inputs exceed 2 GiB")
    return [checked[name] for name in SOURCE_URLS]


def source_date(sources):
    return max(parsedate_to_datetime(source["lastModified"]).date().isoformat() for source in sources)


def _check_response(response, expected_url):
    final_url = response.geturl() if hasattr(response, "geturl") else expected_url
    parsed = urlparse(final_url)
    if response.status != 200 or parsed.scheme != "https" or parsed.hostname != "opendata.astra.admin.ch":
        raise ValueError("Official source request failed or redirected to an unexpected host")


def check_sources(output_dir, normalizer, opener=urlopen, force=False):
    observations = []
    for name, url in SOURCE_URLS.items():
        request = Request(url, method="HEAD", headers={"Accept-Encoding": "identity", "User-Agent": "Carloss-Catalogue/1"})
        with opener(request, timeout=20) as response:
            _check_response(response, url)
            observations.append(_source_info(name, response.headers))
    observations = validate_sources(observations)
    normalizer_sha = digest_file(normalizer, JSON_LIMIT)
    provenance_path = Path(output_dir) / "provenance.json"
    previous = read_json(provenance_path) if provenance_path.exists() else {}
    manifest_path = Path(output_dir) / "manifest.json"
    old_revision = None
    if manifest_path.exists():
        old_revision = unpack_manifest(read_json(manifest_path))["revision"]
    old_sources = {source["name"]: source for source in previous.get("sources", [])}
    changed = any(source_identity(source) != source_identity(old_sources.get(source["name"], {})) for source in observations)
    changed = changed or previous.get("normalizerSHA256") != normalizer_sha
    changed = changed or old_revision is None or previous.get("catalogueRevision") != old_revision
    return {"formatVersion": 1, "sources": observations, "normalizerSHA256": normalizer_sha,
            "fingerprint": hashlib.sha256(canonical([source_identity(source) for source in observations])).hexdigest(),
            "needsBuild": bool(force or changed), "checkedAt": utc_now()}


def csv_header(path):
    with Path(path).open("rb") as handle:
        line = handle.readline(1024 * 1024 + 1)
    if len(line) > 1024 * 1024:
        raise ValueError("CSV header exceeds the size limit")
    try:
        return next(csv.reader(io.StringIO(line.decode("utf-8-sig")), delimiter=";", strict=True))
    except (UnicodeError, csv.Error, StopIteration):
        raise ValueError("Invalid UTF-8 CSV header") from None


def cached_source(source, cache_dir):
    path = Path(cache_dir) / (source["name"] + ".csv")
    metadata = path.with_suffix(".metadata.json")
    try:
        previous = read_json(metadata)
        if source_identity(previous) != source_identity(source) or path.stat().st_size != source["contentLength"]:
            return None
        if digest_file(path) != previous.get("downloadSHA256"):
            return None
        return path, previous
    except (OSError, ValueError):
        return None


def download_source(source, cache_dir, previous_header=None, opener=urlopen):
    reused = cached_source(source, cache_dir)
    if reused:
        path, metadata = reused
        validate_header(source["name"], csv_header(path), previous_header)
        return path, metadata
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (source["name"] + ".csv")
    headers = {"Accept-Encoding": "identity", "User-Agent": "Carloss-Catalogue/1",
               "If-Unmodified-Since": source["lastModified"]}
    if source.get("etag") and not source["etag"].startswith("W/"):
        headers["If-Match"] = source["etag"]
    request = Request(source["url"], headers=headers)
    name = None
    try:
        with opener(request, timeout=30) as response:
            _check_response(response, source["url"])
            received = _source_info(source["name"], response.headers)
            if source_identity(received) != source_identity(source):
                raise ValueError("Source changed between HEAD and download; retry on the next run")
            size, digest, deadline = 0, hashlib.sha256(), time.monotonic() + 15 * 60
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".astra-", delete=False) as handle:
                name = Path(handle.name)
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > source["contentLength"] or size > RAW_LIMIT or time.monotonic() > deadline:
                        raise ValueError("Source download exceeded its size or time limit")
                    digest.update(chunk)
                    handle.write(chunk)
                if size != source["contentLength"]:
                    raise ValueError("Incomplete source download")
            header = csv_header(name)
            validate_header(source["name"], header, previous_header)
            metadata = {**source, "downloadSHA256": digest.hexdigest(), "header": header,
                        "headerSHA256": hashlib.sha256(canonical(header)).hexdigest()}
            os.replace(name, path)
            atomic_write(path.with_suffix(".metadata.json"), canonical(metadata) + b"\n")
            return path, metadata
    finally:
        if name and name.exists():
            name.unlink()


def deterministic_gzip(data):
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=9) as handle:
        handle.write(data)
    return output.getvalue()


def _child_env():
    return {key: value for key, value in os.environ.items() if key != SIGNING_ENV}


def _openssl(arguments, data=None):
    result = subprocess.run(["openssl", *arguments], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=20, env=_child_env())
    if result.returncode:
        raise ValueError("OpenSSL signature operation failed")
    return result.stdout


def sign(payload, private_pem):
    if not isinstance(private_pem, str) or not 64 <= len(private_pem) <= 16_384:
        raise ValueError("A PEM signing secret is required")
    with tempfile.TemporaryDirectory(prefix="catalogue-sign-") as directory:
        key = Path(directory) / "private.pem"
        descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(private_pem)
        public = Path(directory) / "public.pem"
        public.write_bytes(_openssl(["pkey", "-in", str(key), "-pubout"]))
        description = _openssl(["pkey", "-pubin", "-in", str(public), "-text", "-noout"])
        if b"prime256v1" not in description:
            raise ValueError("The signing key must use P-256")
        return _openssl(["dgst", "-sha256", "-sign", str(key)], payload)


def unpack_manifest(wrapper):
    if not isinstance(wrapper, dict) or set(wrapper) != {"formatVersion", "keyID", "payload", "signature"}:
        raise ValueError("Unexpected manifest wrapper")
    if type(wrapper["formatVersion"]) is not int or wrapper["formatVersion"] != 1 or wrapper["keyID"] != KEY_ID:
        raise ValueError("Unsupported manifest format or signing key")
    try:
        encoded = base64.b64decode(wrapper["payload"], validate=True)
        signature = base64.b64decode(wrapper["signature"], validate=True)
    except (ValueError, TypeError):
        raise ValueError("Invalid manifest encoding") from None
    payload = decode_json(encoded)
    expected = {"schemaVersion", "revision", "sourceUpdatedAt", "publishedAt", "file", "sha256", "compressedBytes", "uncompressedBytes", "vehicleCount"}
    if not isinstance(payload, dict) or set(payload) != expected or canonical(payload) != encoded:
        raise ValueError("Unexpected manifest payload")
    revision = payload.get("revision", "")
    if type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1 or not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError("Unsupported schema or revision")
    if payload["sha256"] != revision or payload["file"] != f"catalogue/{revision}.json.gz":
        raise ValueError("Manifest path or hash mismatch")
    for key, minimum, maximum in (("compressedBytes", 1, GZIP_LIMIT), ("uncompressedBytes", 1, JSON_LIMIT), ("vehicleCount", 10_000, 100_000)):
        upper = maximum if key == "vehicleCount" else maximum - 1
        if type(payload[key]) is not int or not minimum <= payload[key] <= upper:
            raise ValueError("Manifest bound exceeded")
    datetime.strptime(payload["sourceUpdatedAt"], "%Y-%m-%d")
    published = datetime.fromisoformat(payload["publishedAt"].replace("Z", "+00:00"))
    if published.tzinfo is None or not payload["publishedAt"].endswith("Z") or not 64 <= len(signature) <= 80:
        raise ValueError("Invalid signature or publication date")
    return payload


def verify_signature(wrapper, public_key):
    unpack_manifest(wrapper)
    encoded = base64.b64decode(wrapper["payload"], validate=True)
    description = _openssl(["pkey", "-pubin", "-in", str(public_key), "-text", "-noout"])
    if b"prime256v1" not in description:
        raise ValueError("The pinned public key must use P-256")
    with tempfile.TemporaryDirectory(prefix="catalogue-verify-") as temporary:
        signature = Path(temporary) / "signature.der"
        signature.write_bytes(base64.b64decode(wrapper["signature"], validate=True))
        _openssl(["dgst", "-sha256", "-verify", str(public_key), "-signature", str(signature)], encoded)


def verify_manifest(output_dir, public_key):
    directory = Path(output_dir)
    wrapper = read_json(directory / "manifest.json")
    payload = unpack_manifest(wrapper)
    verify_signature(wrapper, public_key)
    path = directory / payload["file"]
    if path.stat().st_size != payload["compressedBytes"] or digest_file(path, GZIP_LIMIT) != payload["sha256"]:
        raise ValueError("Catalogue compressed size or digest mismatch")
    with gzip.open(path, "rb") as handle:
        data = handle.read(JSON_LIMIT + 1)
    if len(data) != payload["uncompressedBytes"] or len(data) >= JSON_LIMIT:
        raise ValueError("Catalogue uncompressed size mismatch")
    document = decode_json(data)
    summary = validate_catalogue(document)
    if summary["vehicleCount"] != payload["vehicleCount"] or document["updatedAt"] != payload["sourceUpdatedAt"]:
        raise ValueError("Catalogue manifest identity mismatch")
    return document, payload


def publish_document(document, output_dir, sources, normalizer_sha, private_pem=None, public_key=None, published_at=None):
    directory = Path(output_dir)
    previous, old_payload = None, None
    if (directory / "manifest.json").exists():
        if public_key is None:
            raise ValueError("Verify the previous publication with --public-key before replacing it")
        previous, old_payload = verify_manifest(directory, public_key)
    summary = validate_catalogue(document, previous=previous)
    if sources:
        sources = validate_sources(sources)
        if source_date(sources) != document["updatedAt"]:
            raise ValueError("Catalogue source date does not match the official source observations")
    data = canonical(document)
    compressed = deterministic_gzip(data)
    if len(data) >= JSON_LIMIT or len(compressed) >= GZIP_LIMIT:
        raise ValueError("Catalogue exceeds publication size limits")
    revision = hashlib.sha256(compressed).hexdigest()
    payload = {"schemaVersion": 1, "revision": revision, "sourceUpdatedAt": document["updatedAt"],
               "publishedAt": published_at or utc_now(), "file": f"catalogue/{revision}.json.gz", "sha256": revision,
               "compressedBytes": len(compressed), "uncompressedBytes": len(data), "vehicleCount": summary["vehicleCount"]}
    if old_payload and old_payload["revision"] == revision:
        wrapper = read_json(directory / "manifest.json")
        payload = old_payload
    else:
        payload["publishedAt"] = next_publication_time(payload["publishedAt"], old_payload["publishedAt"] if old_payload else None)
        if document["updatedAt"] > payload["publishedAt"][:10]:
            raise ValueError("Publication time predates the official source date")
        encoded = canonical(payload)
        signature = sign(encoded, private_pem if private_pem is not None else os.environ.get(SIGNING_ENV))
        wrapper = {"formatVersion": 1, "keyID": KEY_ID, "payload": base64.b64encode(encoded).decode("ascii"),
                   "signature": base64.b64encode(signature).decode("ascii")}
        unpack_manifest(wrapper)
    if public_key is not None:
        # Refuse a misconfigured secret before replacing the last good pointer.
        verify_signature(wrapper, public_key)
    old_provenance = read_json(directory / "provenance.json") if (directory / "provenance.json").exists() else {}
    revisions = list(dict.fromkeys([revision, *(old_provenance.get("revisions", []))]))[:3]
    provenance = {"formatVersion": 1, "catalogueRevision": revision, "sourceUpdatedAt": document["updatedAt"],
                  "sources": sources, "normalizerSHA256": normalizer_sha, "summary": summary, "revisions": revisions}
    immutable = directory / payload["file"]
    if immutable.exists() and immutable.read_bytes() != compressed:
        raise ValueError("Immutable revision content mismatch")
    if not immutable.exists():
        atomic_write(immutable, compressed)
    atomic_write(directory / "provenance.json", canonical(provenance) + b"\n")
    # Clients only observe a new revision after all its immutable content exists.
    atomic_write(directory / "manifest.json", canonical(wrapper) + b"\n")
    for path in (directory / "catalogue").glob("*.json.gz"):
        revision_name = path.name.removesuffix(".json.gz")
        if SHA.fullmatch(revision_name) and revision_name not in revisions:
            path.unlink()
    return payload


def publish_from_sources(plan, output_dir, cache_dir, normalizer, public_key, opener=urlopen):
    if plan.get("formatVersion") != 1 or not plan.get("needsBuild"):
        return None
    sources = validate_sources(plan.get("sources"))
    if plan.get("normalizerSHA256") != digest_file(normalizer, JSON_LIMIT):
        raise ValueError("Normalizer changed after the source check")
    provenance_path = Path(output_dir) / "provenance.json"
    previous = read_json(provenance_path) if provenance_path.exists() else {}
    old_sources = {source["name"]: source for source in previous.get("sources", [])}
    paths, observed = {}, []
    for source in sources:
        path, metadata = download_source(source, cache_dir, old_sources.get(source["name"], {}).get("header"), opener)
        paths[source["name"]] = path
        observed.append(metadata)
    with tempfile.TemporaryDirectory(prefix="catalogue-build-") as directory:
        output = Path(directory) / "catalogue.json"
        command = [sys.executable, str(normalizer), "--autos", str(paths["autos"]), "--emissions", str(paths["emissions"]),
                   "--edata", str(paths["edata"]), "--updated-at", source_date(sources), "--output", str(output)]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25 * 60, env=_child_env())
        if result.returncode:
            raise ValueError("The catalogue normalizer failed; no publication was replaced")
        document = read_json(output, JSON_LIMIT)
    return publish_document(document, output_dir, observed, plan["normalizerSHA256"], public_key=public_key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--seed", type=Path)
    action.add_argument("--check-sources", action="store_true")
    action.add_argument("--publish", action="store_true")
    action.add_argument("--verify", action="store_true")
    parser.add_argument("--source-metadata", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "v1")
    parser.add_argument("--normalizer", type=Path, default=ROOT / "tools/build-vehicle-catalog.py")
    parser.add_argument("--cache-dir", type=Path, default=Path(tempfile.gettempdir()) / "carloss-astra-source-cache")
    parser.add_argument("--public-key", type=Path, default=ROOT / "catalogue-v1.pub")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.check_sources:
        plan = check_sources(args.output_dir, args.normalizer, force=args.force)
        if args.plan:
            atomic_write(args.plan, canonical(plan) + b"\n")
        if args.github_output:
            with args.github_output.open("a") as handle:
                handle.write(f"needs-build={str(plan['needsBuild']).lower()}\nfingerprint={plan['fingerprint']}\n")
        print(json.dumps({"needsBuild": plan["needsBuild"], "sourceUpdatedAt": source_date(plan["sources"]), "fingerprint": plan["fingerprint"]}))
    elif args.seed:
        document = read_json(args.seed, JSON_LIMIT)
        sources = validate_sources(read_json(args.source_metadata)["sources"]) if args.source_metadata else []
        normalizer_sha = digest_file(args.normalizer, JSON_LIMIT)
        payload = publish_document(document, args.output_dir, sources, normalizer_sha, public_key=args.public_key)
        print(json.dumps({"revision": payload["revision"], "vehicleCount": payload["vehicleCount"]}))
    elif args.verify:
        _, payload = verify_manifest(args.output_dir, args.public_key)
        print(json.dumps({"verified": True, "revision": payload["revision"], "vehicleCount": payload["vehicleCount"]}))
    else:
        if not args.plan:
            parser.error("--publish requires --plan from --check-sources")
        payload = publish_from_sources(read_json(args.plan), args.output_dir, args.cache_dir, args.normalizer, args.public_key)
        print(json.dumps({"published": payload is not None, "revision": payload["revision"] if payload else None}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        # Never echo environment secrets, source rows or subprocess stderr.
        detail = " ".join(str(error).split())[:500]
        print(f"Catalogue publication failed ({type(error).__name__}): {detail}. The previous manifest is retained.", file=sys.stderr)
        sys.exit(1)
