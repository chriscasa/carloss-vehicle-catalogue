# Carloss vehicle catalogue

Public vehicle specifications for [Carloss](https://github.com/chriscasa/carloss-vehicle-catalogue). This repository contains catalogue data and its publisher. It contains no iPhone app source, app assets, accounts, receipts, routes or user data.

## Feed

The stable manifest is `v1/manifest.json`. It points to an immutable `v1/catalogue/<sha256>.json.gz` file. The SHA-256 covers the compressed file, and both compressed and uncompressed byte counts are signed. The current revision and two previous revisions are retained.

The manifest wrapper contains `formatVersion`, `keyID`, `payload` and `signature`. `payload` is base64-encoded, canonical UTF-8 JSON. `signature` is a base64-encoded ASN.1 DER ECDSA signature over those exact payload bytes, using P-256 and SHA-256. The pinned public key is `catalogue-v1.pub`; its key identifier is `catalogue-v1`.

Signed payload fields:

| Field | Meaning |
| --- | --- |
| `schemaVersion` | Catalogue schema, currently `1` |
| `revision`, `sha256` | SHA-256 of the gzip file |
| `sourceUpdatedAt` | Latest official source modification date, `YYYY-MM-DD` |
| `publishedAt` | Publication time in UTC |
| `file` | Relative immutable path beneath `v1/` |
| `compressedBytes`, `uncompressedBytes` | Exact bounded file sizes |
| `vehicleCount` | Number of validated vehicle variants |

Clients can download the manifest through GitHub's HTTPS raw-file endpoint, verify it with the pinned key, then verify the downloaded payload hash and limits before replacing a local catalogue. A failed check must retain the last valid local catalogue. `sourceUpdatedAt` is not the workflow execution date.

## Sources

Vehicle identity and specifications come from the Swiss Federal Roads Office, **ASTRA / FEDRO**:

- [ASTRA vehicle data](https://www.astra.admin.ch/en/vehicle-data)
- [TAS automobile and emissions exports](https://opendata.astra.admin.ch/ivzod/4000-Typengenehmigungen_TAS/4300-Datensaetze/): `TAS_Automobil.csv` and `TAS_Emission.csv`.
- [Electronic approval export](https://opendata.astra.admin.ch/ivzod/3000-eDatenblatt/eDatenblatt.csv).

The two explicit CUPRA Terramar plug-in variants retain the manufacturer's consumption, system power, 45 L tank, 25.8 kWh gross and 19.7 kWh usable battery facts. References remain attached to those catalogue records. Missing capacities and consumption are left absent. Weighted PHEV figures are not substituted for fuel-only or pure-electric defaults.

This is a catalogue of Swiss approvals and curated manufacturer facts, not every vehicle worldwide. ASTRA and manufacturer source terms and attribution apply to their respective data. VINs, individual approvals, manufacture dates and raw source rows are excluded from published files.

## Daily publication

The workflow checks the three official sources daily at **03:17 UTC** and can be run manually. It compares ETag, Last-Modified and Content-Length with `v1/provenance.json`. An unchanged source set and normalizer require only three HEAD requests; the existing publication remains untouched.

When rebuilding, a temporary Actions cache supplies unchanged input files. Only changed or missing cache inputs are downloaded. A cold cache needs an initial copy of each input because the normalizer joins all three sources. Raw files never enter this repository. Downloads stream to temporary files, enforce HTTPS and the official host, have connection and transfer deadlines, and are bounded to 2 GiB per input and 2 GiB for the complete source set.

Large source files use 4 MiB HTTP ranges with at most four concurrent transfers. Each response must match its exact range and the original source validators; interrupted transfers have bounded retries. Completed files are hashed and their CSV headers checked before entering the reusable cache. The workflow checks its signing key before restoring or downloading source files.

Before publication the pipeline rejects changed established CSV headers, missing required columns, unknown catalogue fields, duplicate IDs, invalid units or standards, invalid capacities, source-date regressions and substantial coverage loss. It validates 10,000–100,000 variants, JSON below 32 MiB and deterministic gzip below 4 MiB. The signed manifest is replaced atomically only after its immutable payload exists and the signature matches the pinned public key. Errors leave the previous publication available. Source headers, hashes and aggregate coverage are recorded in provenance; raw rows are not.

## Maintainer setup

Python 3.11 or later and OpenSSL are required; no Python packages are needed. Place the reviewed normalizer at `tools/build-vehicle-catalog.py`. Its CLI accepts `--autos`, `--emissions`, `--edata`, `--updated-at` and `--output`.

Commit the public P-256 key as `catalogue-v1.pub`. Store the PEM private key only in the repository Actions secret **`CATALOGUE_SIGNING_PRIVATE_KEY`**. The publisher passes no signing secret to the normalizer or OpenSSL environment; signing uses a temporary mode-0600 key file that is removed afterward. Never commit that key or the raw CSV files.

Check official metadata without downloading input bodies:

```sh
python tools/publish_catalogue.py --check-sources --plan /tmp/carloss-catalogue-plan.json
```

Seed an initial publication from an already reviewed catalogue:

```sh
python tools/publish_catalogue.py --seed /path/to/VehicleCatalog.json
```

If the seed was built from the exact observed source versions, add `--source-metadata /tmp/carloss-catalogue-plan.json`. The source dates must match the seed. Do not attach current metadata to an older seed. Without metadata, the first scheduled run performs a full rebuild to establish source provenance.

Build after the metadata check:

```sh
python tools/publish_catalogue.py --publish --plan /tmp/carloss-catalogue-plan.json --cache-dir /tmp/carloss-astra-cache
```

Verify the signed manifest, payload integrity and full catalogue:

```sh
python tools/publish_catalogue.py --verify
python -m unittest discover -s tests -v
```

Review a changed source schema or substantial coverage change before updating the validator. Validation failures should be investigated rather than bypassed by the scheduled workflow.
