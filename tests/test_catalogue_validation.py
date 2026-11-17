"""Publication checks use synthetic normalized vehicles, never raw approvals."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from catalogue_validation import REQUIRED_HEADERS, SOURCE, SOURCE_URL, validate_catalogue, validate_header


def catalogue(count=10_000):
    return {"source": SOURCE, "sourceURL": SOURCE_URL, "updatedAt": "2026-01-01", "vehicles": [
        {"id": f"astra-{index:016x}", "make": f"Make {index % 20}", "model": "Model", "energy": "Petrol",
         "consumption": 6.2, "consumptionStandard": "WLTP",
         "specs": {"sourceURL": SOURCE_URL, "fuelCombined": measure(6.2, "L/100 km", "WLTP combined")}}
        for index in range(count)]}


def measure(value, unit, standard):
    return {"min": value, "max": value, "unit": unit, "standard": standard}


class CatalogueValidationTests(unittest.TestCase):
    def test_valid_catalogue_returns_coverage_counts(self):
        result = validate_catalogue(catalogue())
        self.assertEqual(result["vehicleCount"], 10_000)
        self.assertEqual(result["makeCount"], 20)
        self.assertEqual(result["energyCounts"], {"Petrol": 10_000})
        self.assertEqual(result["withConsumption"], 10_000)
        self.assertEqual(result["withFuelRatings"], 10_000)
        self.assertEqual(result["withElectricRatings"], 0)

    def test_duplicate_and_raw_identifiers_are_rejected(self):
        for identifier in ("WVWZZZ1JZXW000001", "astra-not-a-hash", "astra-0000000000000001"):
            with self.subTest(identifier=identifier):
                value = catalogue()
                value["vehicles"][0]["id"] = identifier
                with self.assertRaises(ValueError):
                    validate_catalogue(value)

    def test_raw_private_or_unknown_schema_fields_are_rejected(self):
        for field in ("vin", "registrationDate", "owner", "bodyType"):
            value = catalogue()
            value["vehicles"][0][field] = "unexpected raw field"
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_catalogue(value)
        value = catalogue()
        value["schemaVersion"] = 2
        with self.assertRaises(ValueError):
            validate_catalogue(value)

    def test_nonfinite_boolean_negative_and_wrong_unit_values_are_rejected(self):
        for amount in (float("nan"), float("inf"), True, -2, 0, 100):
            value = catalogue()
            value["vehicles"][0]["consumption"] = amount
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                validate_catalogue(value)
        value = catalogue()
        value["vehicles"][0]["specs"]["fuelCombined"]["unit"] = "Wh/km"
        with self.assertRaises(ValueError):
            validate_catalogue(value)

    def test_plugin_weighted_scalar_default_is_rejected(self):
        value = catalogue()
        car = value["vehicles"][0]
        car["energy"] = "Plug-in hybrid"
        car["consumptionStandard"] = "WLTP weighted"
        car["specs"]["fuelCombined"]["standard"] = "WLTP weighted"
        with self.assertRaises(ValueError):
            validate_catalogue(value)
        car["consumptionStandard"] = "WLTP charge sustaining"
        self.assertEqual(validate_catalogue(value)["vehicleCount"], 10_000)

    def test_gross_and_usable_capacity_have_separate_sources_and_bounds(self):
        value = catalogue()
        car = value["vehicles"][0]
        car["specs"].update(batteryKWh=measure(25.8, "kWh", "Gross capacity"),
                             usableBatteryKWh=measure(19.7, "kWh", "Net usable capacity"))
        with self.assertRaises(ValueError):
            validate_catalogue(value)
        car["specs"]["capacitySourceURL"] = "https://manufacturer.example/technical.pdf"
        validate_catalogue(value)
        car["specs"]["usableBatteryKWh"]["max"] = 26
        with self.assertRaises(ValueError):
            validate_catalogue(value)

    def test_bounds_standards_and_duplicate_additional_measures_are_checked(self):
        value = catalogue()
        car = value["vehicles"][0]
        car["specs"]["fuelCombined"]["max"] = 6.0
        with self.assertRaises(ValueError):
            validate_catalogue(value)
        car["specs"]["fuelCombined"] = measure(6.2, "L/100 km", "NEDC combined")
        car["specs"]["otherConsumption"] = [{"id": "fuel-high", "title": "Fuel high speed", "value": measure(6, "L/100 km", "WLTP high")}] * 2
        with self.assertRaises(ValueError):
            validate_catalogue(value)

    def test_malformed_typed_json_raises_validation_errors(self):
        for field, invalid in (("energy", ["Petrol"]), ("make", {}), ("powerKW", 10 ** 1_000)):
            value = catalogue()
            value["vehicles"][0][field] = invalid
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_catalogue(value)
        value = catalogue()
        value["vehicles"][0]["specs"]["fuelCombined"]["standard"] = ["WLTP combined"]
        with self.assertRaises(ValueError):
            validate_catalogue(value)

    def test_wrong_source_future_date_and_date_regression_are_rejected(self):
        value = catalogue()
        value["sourceURL"] = "http://www.astra.admin.ch/en/vehicle-data"
        with self.assertRaises(ValueError):
            validate_catalogue(value)
        value = catalogue()
        value["updatedAt"] = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
        with self.assertRaises(ValueError):
            validate_catalogue(value)
        value = catalogue()
        value["updatedAt"] = "2026-02-30"
        with self.assertRaises(ValueError):
            validate_catalogue(value)
        previous = catalogue()
        value = catalogue()
        value["updatedAt"] = "2025-12-31"
        with self.assertRaises(ValueError):
            validate_catalogue(value, previous)

    def test_count_limits_and_vehicle_coverage_regression_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_catalogue(catalogue(9_999))
        with self.assertRaises(ValueError):
            validate_catalogue(catalogue(100_001))
        with self.assertRaises(ValueError):
            validate_catalogue(catalogue(10_000), catalogue(11_112))

    def test_make_coverage_cannot_silently_disappear(self):
        previous = catalogue()
        changed = deepcopy(previous)
        for car in changed["vehicles"]:
            if car["make"] in {"Make 0", "Make 1", "Make 2"}:
                car["make"] = "Make 3"
        with self.assertRaises(ValueError):
            validate_catalogue(changed, previous)

    def test_energy_ratings_consumption_and_identity_churn_are_checked_with_unchanged_total(self):
        previous = catalogue()
        changed = deepcopy(previous)
        for car in changed["vehicles"][:2_001]:
            car["energy"] = "Diesel"
        with self.assertRaises(ValueError):
            validate_catalogue(changed, previous)
        changed = deepcopy(previous)
        for car in changed["vehicles"][:2_001]:
            del car["specs"]["fuelCombined"]
        with self.assertRaises(ValueError):
            validate_catalogue(changed, previous)
        changed = deepcopy(previous)
        for index, car in enumerate(changed["vehicles"][:2_001]):
            car["id"] = f"astra-{index + 20_000:016x}"
        with self.assertRaises(ValueError):
            validate_catalogue(changed, previous)
        changed = deepcopy(previous)
        for car in changed["vehicles"][:2_001]:
            del car["consumption"]
            del car["consumptionStandard"]
        with self.assertRaises(ValueError):
            validate_catalogue(changed, previous)

    def test_curated_supplements_keep_stable_identity_and_net_capacity(self):
        value = catalogue(10_001)
        value["vehicles"][-1] = {"id": "cupra-terramar-phev-150", "make": "CUPRA", "model": "Terramar",
            "energy": "Plug-in hybrid", "variant": "1.5 e-HYBRID 204 PS", "enginePowerKW": 110,
            "systemPowerKW": 150, "sourceURL": "https://www.cupra.com/technical.pdf", "specs": {
                "sourceURL": "https://www.cupra.com/technical.pdf", "capacitySourceURL": "https://www.cupra.com/capacity.pdf",
                "batteryKWh": measure(25.8, "kWh", "Gross capacity"), "usableBatteryKWh": measure(19.7, "kWh", "Net usable capacity"),
                "fuelTankLitres": measure(45, "L", "Fuel tank capacity")}}
        validate_catalogue(value)
        changed = deepcopy(value)
        changed["vehicles"][-1]["specs"]["usableBatteryKWh"] = measure(25.8, "kWh", "Net usable capacity")
        with self.assertRaises(ValueError):
            validate_catalogue(changed, value)
        changed = deepcopy(value)
        changed["vehicles"].pop()
        with self.assertRaises(ValueError):
            validate_catalogue(changed, value)

    def test_all_required_source_columns_and_complete_pinned_header_are_checked(self):
        for name, columns in REQUIRED_HEADERS.items():
            header = sorted(columns)
            validate_header(name, header)
            validate_header(name, header, header.copy())
            validate_header(name, header + ["officialOptionalColumn"])
            for changed in (header[:-1], header + [header[0]], header + [""], header + ["newColumn"], list(reversed(header))):
                with self.subTest(name=name, changed=changed[-2:]), self.assertRaises(ValueError):
                    validate_header(name, changed, header)


if __name__ == "__main__":
    unittest.main()
