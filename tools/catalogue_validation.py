"""Fail-closed checks for the public, normalized ASTRA vehicle catalogue.

This module accepts only the app's published vehicle facts. Raw approval fields,
VINs, registration dates and other additions require an explicit schema review.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timezone
import math
import re
from urllib.parse import urlsplit

MINIMUM_VEHICLES = 10_000
MAXIMUM_VEHICLES = 100_000
SOURCE = "ASTRA / FEDRO"
SOURCE_URL = "https://www.astra.admin.ch/en/vehicle-data"
ENERGIES = frozenset({"Petrol", "Diesel", "Electric", "Plug-in hybrid"})
DOCUMENT_FIELDS = frozenset({"source", "sourceURL", "updatedAt", "vehicles"})
VEHICLE_FIELDS = frozenset({"id", "make", "model", "energy", "variant", "powerKW", "enginePowerKW",
    "systemPowerKW", "sourceURL", "consumption", "consumptionStandard", "electricConsumption", "specs"})
SPEC_FIELDS = frozenset({"fuelCombined", "fuelDischarged", "electricCombined", "electricRange", "batteryKWh",
    "fuelTankLitres", "usableBatteryKWh", "capacitySourceURL", "engineCC", "gearbox", "drive", "sourceURL", "otherConsumption"})
COMBINED_STANDARDS = frozenset({"WLTP combined", "WLTP weighted", "NEDC combined", "NEDC weighted"})
RANGE_RULES = {
    "fuelCombined": ("L/100 km", COMBINED_STANDARDS, 100),
    "fuelDischarged": ("L/100 km", {"WLTP charge sustaining"}, 100),
    "electricCombined": ("kWh/100 km", COMBINED_STANDARDS, 100),
    "electricRange": ("km", {"WLTP electric", "NEDC electric"}, 2_000),
    "batteryKWh": ("kWh", {"Gross capacity"}, 500),
    "usableBatteryKWh": ("kWh", {"Net usable capacity"}, 500),
    "fuelTankLitres": ("L", {"Fuel tank capacity"}, 500),
    "engineCC": ("cc", {"Engine displacement"}, 15_000),
}
OTHER_RULES = {
    "electric-city-range": ("km", {"WLTP city"}, 2_000),
    "all-electric-range": ("km", {"WLTP all-electric"}, 2_000),
    "nedc-electric-combined": ("kWh/100 km", {"NEDC combined", "NEDC weighted"}, 100),
    "nedc-fuel-weighted": ("L/100 km", {"NEDC weighted"}, 100),
    "fuel-charge-depleting": ("L/100 km", {"WLTP charge depleting"}, 100),
    "electric-charge-depleting": ("kWh/100 km", {"WLTP charge depleting"}, 100),
}
for phase in ("low", "medium", "high", "extra-high"):
    OTHER_RULES["fuel-" + phase] = ("L/100 km", {"WLTP " + phase}, 100)
    OTHER_RULES["electric-" + phase] = ("kWh/100 km", {"WLTP " + phase}, 100)
    OTHER_RULES["fuel-discharged-" + phase] = ("L/100 km", {"WLTP charge sustaining"}, 100)
    OTHER_RULES["electric-charged-" + phase] = ("kWh/100 km", {"WLTP charge depleting"}, 100)
OTHER_RULES["electric-charged-city"] = ("kWh/100 km", {"WLTP charge depleting"}, 100)
for phase in ("city", "extra-urban", "combined"):
    standards = {"NEDC " + phase}
    if phase == "combined":
        standards.add("NEDC weighted")
    OTHER_RULES["nedc-fuel-" + phase] = ("L/100 km", standards, 100)

# Columns consumed by the committed normalizer. The first successful import
# pins the complete header separately; subsequent changes require review even
# if the normalizer would otherwise silently ignore a renamed optional field.
REQUIRED_HEADERS = {
    "autos": frozenset({"chVehicleType", "vehicleCategoryCode", "chFuelType", "offVehicleChargingIndex",
        "makeName", "commercialName", "chTypeApprovalNumber", "engineCapacity", "maximumNetPower", "driveType"}
        | {f"gearboxEmissions{i}.chGearboxType" for i in range(1, 5)}),
    "emissions": frozenset({"chTypeApprovalNumber", "primaryFuelConsumptionWltp", "electricEnergyConsumptionWltp",
        "primaryFuelConsumption", "electricEnergyConsumption", "electricDriveModeRangeFromWltp",
        "electricDriveModeRangeToWltp", "electricDriveModeRangeFrom", "electricDriveModeRangeTo"}),
    "edata": frozenset({"0.4. VehicleCategoryCode", "N3 Tstof", "23.1. ClassOfHybridVehicleCode",
        "23. PureElectricVehIndicator", "0.1. Make", "0.2.1. CommercialName", "25. EngineCapacity",
        "28. GearboxTypeCode", "N2 Antri", "27.1. MaximumNetPower", "27.3. MaximumNetPowerElectricEngine",
        "49. WLTPElecEnergyConsPureElectric", "49. WLTPCombinedFuelConsChargeSust", "49. WLTPCombinedFuelCons",
        "49. WLTPElectricRangePureElectric", "49. WLTPElectricRangeCityPureElec", "49. WLTPWeightedCombinedFuelCons",
        "49. WLTPElectEnergyConsExternCharg", "49. ElectricEnergyConsWeightedComb", "49. WLTPElectricRangeExternCharg",
        "49. WLTPElectricRangeCityExtCharg", "49. WLTPAllElectricRangeExternCharg", "49. WLTPCombElEnergyConsExtCharg",
        "49. WLTPCombinedFuelConsChargeDepl", "49. WeightedCombinedFuelCons", "49. CombinedFuelConsumption"}
        | {"49. " + suffix for suffix in ("WLTPLowElecEnergyConsPureElec", "WLTPMediumElEnergyConsPureElec",
            "WLTPHighElecEnergyConsPureElec", "WLTPExtrHighElecEnConsPureElec", "WLTPLowFuelConsChargeSust",
            "WLTPMediumFuelConsChargeSust", "WLTPHighFuelConsChargeSust", "WLTPExtraHighFuelConsChargSust",
            "WLTPLowElecEnergyConsExtCharg", "WLTPMediumElEnergyConsExtCharg", "WLTPHighElEnergyConsExtCharg",
            "WLTPExtrHighElecEnConsExtCharg", "WLTPCityElecEnergyConsExtCharg", "WLTPLowFuelConsumption",
            "WLTPMediumFuelConsumption", "WLTPHighFuelConsumption", "WLTPExtraHighFuelConsumption",
            "UrbanConditionsFuelConsumption", "ExtraUrbanConditionsFuelCons")}),
}


def _fail(message: str) -> None:
    raise ValueError(message)


def _mapping(value, allowed, label):
    if not isinstance(value, dict) or set(value) - allowed:
        _fail(f"{label}: unsupported schema fields")
    return value


def _text(value, label, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        _fail(f"{label}: invalid text")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail(f"{label}: control character")
    return value


def _number(value, label, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(f"{label}: invalid number")
    try:
        valid = math.isfinite(value) and 0 < value < maximum
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        _fail(f"{label}: invalid number")
    return value


def _url(value, label):
    parsed = urlsplit(_text(value, label, 2_048))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        _fail(f"{label}: expected a public HTTPS source URL")


def _source_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail("updatedAt: expected YYYY-MM-DD")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        _fail("updatedAt: invalid date")
    if result > datetime.now(timezone.utc).date():
        _fail("updatedAt: future source date")
    return result


def _range(value, rule, label):
    _mapping(value, {"min", "max", "unit", "standard"}, label)
    if set(value) != {"min", "max", "unit", "standard"}:
        _fail(f"{label}: incomplete range")
    unit, standards, maximum = rule
    lower = _number(value["min"], label + ".min", maximum)
    upper = _number(value["max"], label + ".max", maximum)
    _text(value["unit"], label + ".unit", 64)
    _text(value["standard"], label + ".standard", 128)
    if lower > upper or value["unit"] != unit or value["standard"] not in standards:
        _fail(f"{label}: invalid bounds, unit or standard")


def _specifications(value, energy):
    _mapping(value, SPEC_FIELDS, "specs")
    if not value:
        _fail("specs: empty")
    _url(value.get("sourceURL"), "specs.sourceURL")
    for key, rule in RANGE_RULES.items():
        if key in value:
            _range(value[key], rule, "specs." + key)
    for key in ("sourceURL", "capacitySourceURL"):
        if key in value:
            _url(value[key], "specs." + key)
    for key in ("gearbox", "drive"):
        if key in value:
            _text(value[key], "specs." + key, 256)
    if any(key in value for key in ("batteryKWh", "usableBatteryKWh", "fuelTankLitres")) and "capacitySourceURL" not in value:
        _fail("specs: capacities need a separate published capacity source")
    if "usableBatteryKWh" in value and "batteryKWh" in value and value["usableBatteryKWh"]["max"] > value["batteryKWh"]["max"]:
        _fail("specs: usable capacity exceeds gross capacity")
    if energy == "Electric" and any(key in value for key in ("fuelCombined", "fuelDischarged", "fuelTankLitres")):
        _fail("specs: electric vehicle cannot have fuel consumption or tank capacity")
    if "fuelDischarged" in value and energy != "Plug-in hybrid":
        _fail("specs: charge-sustaining consumption requires a plug-in hybrid")
    if energy == "Plug-in hybrid" and "fuelCombined" in value and not value["fuelCombined"]["standard"].endswith("weighted"):
        _fail("specs: plug-in combined fuel must retain its weighted qualification")
    if "otherConsumption" in value:
        extras = value["otherConsumption"]
        if not isinstance(extras, list) or len(extras) > 64:
            _fail("specs.otherConsumption: invalid list")
        ids = set()
        for measure in extras:
            _mapping(measure, {"id", "title", "value"}, "otherConsumption")
            if set(measure) != {"id", "title", "value"}:
                _fail("otherConsumption: incomplete measure")
            _text(measure["id"], "otherConsumption.id", 128)
            if measure["id"] not in OTHER_RULES or measure["id"] in ids:
                _fail("otherConsumption: invalid or duplicate measure")
            ids.add(measure["id"])
            _text(measure["title"], "otherConsumption.title", 256)
            _range(measure["value"], OTHER_RULES[measure["id"]], "otherConsumption." + measure["id"])


def _curated(vehicle):
    """Keep the manufacturer's separately sourced gross/net capacity facts."""
    identifier = vehicle["id"]
    if not identifier.startswith("cupra-terramar-phev-"):
        return
    expected = {"cupra-terramar-phev-150": (150, 110), "cupra-terramar-phev-200": (200, 130)}
    if identifier not in expected:
        _fail("curated supplement: unknown stable identifier")
    system, engine = expected[identifier]
    if (vehicle["make"], vehicle["model"], vehicle["energy"], vehicle.get("systemPowerKW"), vehicle.get("enginePowerKW")) != ("CUPRA", "Terramar", "Plug-in hybrid", system, engine):
        _fail("curated supplement: manufacturer identity or power changed")
    specs = vehicle.get("specs", {})
    for key, expected_value in (("batteryKWh", 25.8), ("usableBatteryKWh", 19.7), ("fuelTankLitres", 45)):
        if specs.get(key, {}).get("min") != expected_value or specs.get(key, {}).get("max") != expected_value:
            _fail("curated supplement: published capacity changed or disappeared")
    if "sourceURL" not in vehicle or "variant" not in vehicle:
        _fail("curated supplement: missing manufacturer source or variant")


def _validate_document(document):
    _mapping(document, DOCUMENT_FIELDS, "catalogue")
    if set(document) != DOCUMENT_FIELDS or document.get("source") != SOURCE or document.get("sourceURL") != SOURCE_URL:
        _fail("catalogue: unsupported document/source schema")
    _source_date(document["updatedAt"])
    vehicles = document["vehicles"]
    if not isinstance(vehicles, list) or not MINIMUM_VEHICLES <= len(vehicles) <= MAXIMUM_VEHICLES:
        _fail("catalogue: vehicle count outside publication bounds")
    by_id = {}
    makes = set()
    energy_counts = Counter()
    coverage = Counter()
    for vehicle in vehicles:
        _mapping(vehicle, VEHICLE_FIELDS, "vehicle")
        if not {"id", "make", "model", "energy"}.issubset(vehicle):
            _fail("vehicle: missing identity")
        identifier = _text(vehicle["id"], "vehicle.id", 128)
        if not re.fullmatch(r"astra-[0-9a-f]{16}|cupra-terramar-phev-(?:150|200)", identifier) or identifier in by_id:
            _fail("vehicle: invalid or duplicate normalized identifier")
        _text(vehicle["make"], "vehicle.make", 256)
        _text(vehicle["model"], "vehicle.model", 512)
        _text(vehicle["energy"], "vehicle.energy", 64)
        if vehicle["energy"] not in ENERGIES:
            _fail("vehicle: unsupported energy")
        for key in ("variant",):
            if key in vehicle:
                _text(vehicle[key], "vehicle." + key, 512)
        for key in ("sourceURL",):
            if key in vehicle:
                _url(vehicle[key], "vehicle." + key)
        for key in ("powerKW", "enginePowerKW", "systemPowerKW"):
            if key in vehicle:
                _number(vehicle[key], "vehicle." + key, 3_000)
        if "consumption" in vehicle or "electricConsumption" in vehicle:
            expected_standard = "WLTP charge sustaining" if vehicle["energy"] == "Plug-in hybrid" else "WLTP"
            if vehicle.get("consumptionStandard") != expected_standard:
                _fail("vehicle: unsafe or missing scalar consumption standard")
            for key in ("consumption", "electricConsumption"):
                if key in vehicle:
                    _number(vehicle[key], "vehicle." + key, 100)
            if "electricConsumption" in vehicle and vehicle["energy"] != "Electric":
                _fail("vehicle: ambiguous scalar electric consumption")
        elif "consumptionStandard" in vehicle:
            _fail("vehicle: consumption standard has no consumption")
        if "specs" in vehicle:
            _specifications(vehicle["specs"], vehicle["energy"])
        _curated(vehicle)
        by_id[identifier] = vehicle
        makes.add(vehicle["make"])
        energy_counts[vehicle["energy"]] += 1
        coverage["withConsumption"] += "consumption" in vehicle
        coverage["withFuelRatings"] += "fuelCombined" in vehicle.get("specs", {})
        coverage["withElectricRatings"] += "electricCombined" in vehicle.get("specs", {})
    summary = {"vehicleCount": len(vehicles), "makeCount": len(makes), "energyCounts": dict(sorted(energy_counts.items())),
               **{key: coverage[key] for key in ("withConsumption", "withFuelRatings", "withElectricRatings")}}
    return summary, by_id, makes


def validate_catalogue(document: dict, previous: dict | None = None) -> dict:
    """Return publication counts, or reject unsafe facts and coverage regressions."""
    summary, by_id, makes = _validate_document(document)
    if previous is not None:
        old, old_by_id, old_makes = _validate_document(previous)
        if document["updatedAt"] < previous["updatedAt"]:
            _fail("catalogue: source date regressed")
        if summary["vehicleCount"] < old["vehicleCount"] * 0.90:
            _fail("catalogue: vehicle coverage fell by more than 10%")
        if len(makes & old_makes) < len(old_makes) * 0.90:
            _fail("catalogue: make coverage fell by more than 10%")
        if len(by_id.keys() & old_by_id.keys()) < len(old_by_id) * 0.80:
            _fail("catalogue: normalized identities changed by more than 20%")
        for energy, count in old["energyCounts"].items():
            if count >= 100 and summary["energyCounts"].get(energy, 0) < count * 0.80:
                _fail("catalogue: energy coverage fell by more than 20%")
        for key in ("withConsumption", "withFuelRatings", "withElectricRatings"):
            if old[key] >= 100 and summary[key] < old[key] * 0.80:
                _fail("catalogue: specification coverage fell by more than 20%")
        for identifier in old_by_id:
            if identifier.startswith("cupra-terramar-phev-") and identifier not in by_id:
                _fail("catalogue: curated manufacturer supplement disappeared")
    return summary


def validate_header(name: str, header: list[str], previous_header: list[str] | None = None) -> None:
    """Pin complete source headers while enforcing the normalizer's used fields."""
    if name not in REQUIRED_HEADERS or not isinstance(header, list) or not 1 <= len(header) <= 4_096:
        _fail("source: unknown input or invalid header")
    for field in header:
        _text(field, "source header", 512)
    if len(set(header)) != len(header) or not REQUIRED_HEADERS[name].issubset(header):
        _fail("source: duplicate or missing required columns")
    if previous_header is not None and header != previous_header:
        _fail("source: header changed; review the normalizer before publishing")
