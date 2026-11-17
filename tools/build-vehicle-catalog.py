#!/usr/bin/env python3
"""Build the bundled offline catalog from official Swiss open vehicle data.

Raw CSVs are build inputs only. They contain substantially more data than the app
needs and must not be copied into the app bundle. See VehicleCatalog-Sources.md.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from itertools import chain
from pathlib import Path

BASE_URL = "https://opendata.astra.admin.ch/ivzod/"
TAS_URL = BASE_URL + "4000-Typengenehmigungen_TAS/4300-Datensaetze/"
EDATA_URL = BASE_URL + "3000-eDatenblatt/eDatenblatt.csv"
ROOT = Path(__file__).resolve().parents[1]
FUEL_ENERGY = {"B": "Petrol", "D": "Diesel", "E": "Electric", "C": "Petrol", "F": "Diesel"}
MAKE_NAMES = {"VW": "Volkswagen", "MERCEDES BENZ": "Mercedes-Benz", "MERCEDES-BENZ": "Mercedes-Benz",
              "CUPRA": "CUPRA", "BMW": "BMW", "MINI": "MINI", "MG": "MG", "BYD": "BYD",
              "DS": "DS", "GMC": "GMC", "JAC": "JAC", "KGM": "KGM", "SEAT": "SEAT"}


def text(value: str | None) -> str:
    return " ".join(str(value or "").split())


def make_name(value: str) -> str:
    clean = text(value)
    return MAKE_NAMES.get(clean.upper(), clean.title() if clean.isupper() else clean)


def model_name(make: str, value: str | None) -> str:
    clean = text(value)
    # This exact manufacturer family is verified by CUPRA's publication and
    # unifies the all-caps CoC name with its named powertrain supplements.
    if make == "CUPRA" and clean.upper() in {"TERRAMAR", "CUPRA TERRAMAR"}:
        return "Terramar"
    return clean


def number(value: str | None) -> float | None:
    try:
        result = float(text(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return round(result, 3) if math.isfinite(result) and result > 0 else None


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle, delimiter=";")


def unique_number(values) -> float | None:
    unique = set(values)
    return next(iter(unique)) if len(unique) == 1 and None not in unique else None


def energy_for(fuel: str, plugin: bool) -> str | None:
    return "Plug-in hybrid" if plugin and fuel in {"C", "F", "B", "D"} else FUEL_ENERGY.get(fuel)


def rating(value, unit, standard, maximum=10000, scale=1):
    amount = number(value)
    if amount is None or amount >= maximum:
        return None
    value = round(amount * scale, 3)
    return {"min": value, "max": value, "unit": unit, "standard": standard}


def add_rating(specs, key, value, unit, standard, maximum=10000, scale=1):
    measure = rating(value, unit, standard, maximum, scale)
    if measure is not None:
        specs[key] = measure


def joined(values):
    return " / ".join(sorted({text(value) for value in values if text(value)})) or None


def gearbox_name(value):
    value = text(value)
    if not value:
        return None
    kind = {"m": "Manual", "a": "Automatic", "i": "Automatic", "c": "CVT"}.get(value[0].lower())
    return kind


def drive_name(value):
    return {"V": "Front-wheel drive", "H": "Rear-wheel drive", "A": "All-wheel drive"}.get(text(value).upper())


def merge_range(target, incoming):
    if target is None:
        return dict(incoming)
    if target["unit"] == incoming["unit"] and target["standard"] == incoming["standard"]:
        target["min"] = min(target["min"], incoming["min"])
        target["max"] = max(target["max"], incoming["max"])
    return target


def merge_specs(target, incoming):
    for key, value in incoming.items():
        if key == "otherConsumption":
            current = {item["id"]: item for item in target.get(key, [])}
            for item in value:
                if item["id"] in current:
                    current[item["id"]]["value"] = merge_range(current[item["id"]]["value"], item["value"])
                else:
                    current[item["id"]] = {**item, "value": dict(item["value"])}
            target[key] = sorted(current.values(), key=lambda item: item["id"])
        elif isinstance(value, dict) and "min" in value:
            if key not in target:
                target[key] = dict(value)
            elif target[key]["standard"] == value["standard"]:
                target[key] = merge_range(target[key], value)
            elif value["standard"].startswith("WLTP") and not target[key]["standard"].startswith("WLTP"):
                target[key] = dict(value)
        elif value:
            if key in {"gearbox", "drive"}:
                target[key] = joined(target.get(key, "").split(" / ") + value.split(" / "))
            else:
                target[key] = value
    return target


def extra_rating(specs, key, title, value, unit, standard, maximum=10000, scale=1):
    measure = rating(value, unit, standard, maximum, scale)
    if measure is not None:
        specs.setdefault("otherConsumption", []).append({"id": key, "title": title, "value": measure})


def tas_records(autos: Path, emissions: Path):
    # There may be up to four gearbox rows per approval. Never select an
    # arbitrary gearbox's consumption when the approval covers differing values.
    quotes = defaultdict(lambda: {"fuel": set(), "electric": set(), "specs": {}})
    for row in read_csv(emissions):
        key = text(row.get("chTypeApprovalNumber"))
        quotes[key]["fuel"].add(number(row.get("primaryFuelConsumptionWltp")))
        quotes[key]["electric"].add(number(row.get("electricEnergyConsumptionWltp")))
        specs = {}
        add_rating(specs, "fuelCombined", row.get("primaryFuelConsumptionWltp"), "L/100 km", "WLTP combined", 100)
        if "fuelCombined" not in specs:
            add_rating(specs, "fuelCombined", row.get("primaryFuelConsumption"), "L/100 km", "NEDC combined", 100)
        extra_rating(specs, "nedc-fuel-combined", "Combined fuel", row.get("primaryFuelConsumption"), "L/100 km", "NEDC combined", 100)
        # CoC electric consumption is reported in Wh/km. Values outside the
        # passenger-car range are omitted, never guessed as another unit.
        electric = number(row.get("electricEnergyConsumptionWltp"))
        if electric is not None and electric >= 40:
            add_rating(specs, "electricCombined", electric, "kWh/100 km", "WLTP combined", 1000, 0.1)
        legacy_electric = number(row.get("electricEnergyConsumption"))
        if legacy_electric is not None and legacy_electric >= 40:
            extra_rating(specs, "nedc-electric-combined", "Combined electricity", legacy_electric, "kWh/100 km", "NEDC combined", 1000, 0.1)
            if "electricCombined" not in specs:
                add_rating(specs, "electricCombined", legacy_electric, "kWh/100 km", "NEDC combined", 1000, 0.1)
        for key_range in ["electricDriveModeRangeFromWltp", "electricDriveModeRangeToWltp"]:
            value = rating(row.get(key_range), "km", "WLTP electric", 2000)
            if value: specs["electricRange"] = merge_range(specs.get("electricRange"), value)
        if "electricRange" not in specs:
            for key_range in ["electricDriveModeRangeFrom", "electricDriveModeRangeTo"]:
                value = rating(row.get(key_range), "km", "NEDC electric", 2000)
                if value: specs["electricRange"] = merge_range(specs.get("electricRange"), value)
        merge_specs(quotes[key]["specs"], specs)
    for row in read_csv(autos):
        if row.get("chVehicleType") != "10" and row.get("vehicleCategoryCode") not in {"M1", "M1G"}:
            continue
        fuel = text(row.get("chFuelType"))
        plugin = text(row.get("offVehicleChargingIndex")).upper() == "TRUE"
        energy = energy_for(fuel, plugin)
        if not energy:
            continue
        make = make_name(row.get("makeName", ""))
        model = model_name(make, row.get("commercialName"))
        if not make or not model:
            continue
        approval = text(row.get("chTypeApprovalNumber"))
        quote = quotes.get(approval, {})
        consumption = unique_number(quote.get("fuel", set()))
        # Keep old scalar defaults conservative. The full published ranges,
        # including electrical ratings in CoC units, are exposed in specs.
        # They are not silently substituted for a driver's personal average.
        if energy in {"Electric", "Plug-in hybrid"}:
            consumption = None
        record = {"make": make, "model": model, "energy": energy}
        specs = merge_specs({}, quote.get("specs", {}))
        if energy == "Electric":
            specs.pop("fuelCombined", None)
            if "otherConsumption" in specs:
                specs["otherConsumption"] = [m for m in specs["otherConsumption"] if "fuel" not in m["id"]]
        if plugin:
            for measure_key in ["fuelCombined", "electricCombined"]:
                if measure_key in specs:
                    specs[measure_key] = {**specs[measure_key], "standard": specs[measure_key]["standard"].replace("combined", "weighted")}
            if "otherConsumption" in specs:
                specs["otherConsumption"] = [{**m, "value": {**m["value"], "standard": m["value"]["standard"].replace("combined", "weighted")}} for m in specs["otherConsumption"]]
        add_rating(specs, "engineCC", row.get("engineCapacity"), "cc", "Engine displacement", 15000)
        gearbox = joined([gearbox_name(row.get(f"gearboxEmissions{i}.chGearboxType")) for i in range(1, 5)])
        if gearbox: specs["gearbox"] = gearbox
        drive = drive_name(row.get("driveType"))
        if drive: specs["drive"] = drive
        if specs:
            specs["sourceURL"] = "https://www.astra.admin.ch/en/vehicle-data"
            record["specs"] = specs
        power = number(row.get("maximumNetPower"))
        if power is not None:
            record["enginePowerKW" if fuel in {"C", "F"} else "powerKW"] = power
        if consumption is not None:
            record.update(consumption=consumption, consumptionStandard="WLTP")
        yield record


def edata_records(path: Path):
    for row in read_csv(path):
        if text(row.get("0.4. VehicleCategoryCode")) not in {"M1", "M1G"}:
            continue
        fuel = text(row.get("N3 Tstof"))
        hybrid_class = text(row.get("23.1. ClassOfHybridVehicleCode"))
        plugin = hybrid_class == "OVC-HEV"
        pure_electric = text(row.get("23. PureElectricVehIndicator")).upper() == "Y"
        energy = "Electric" if pure_electric else energy_for(fuel, plugin)
        if not energy:
            continue
        make = make_name(row.get("0.1. Make", ""))
        model = model_name(make, row.get("0.2.1. CommercialName"))
        if not make or not model:
            continue
        record = {"make": make, "model": model, "energy": energy}
        specs = {}
        add_rating(specs, "engineCC", row.get("25. EngineCapacity"), "cc", "Engine displacement", 15000)
        gearbox = gearbox_name(row.get("28. GearboxTypeCode"))
        if gearbox: specs["gearbox"] = gearbox
        drive = drive_name(row.get("N2 Antri"))
        if drive: specs["drive"] = drive
        engine_power = number(row.get("27.1. MaximumNetPower"))
        electric_power = number(row.get("27.3. MaximumNetPowerElectricEngine"))
        if pure_electric:
            if electric_power is not None:
                record["powerKW"] = electric_power
            electric = number(row.get("49. WLTPElecEnergyConsPureElectric"))
            # Reject implausible source values instead of trying to repair
            # decimal or unit mistakes (one source model has 1990 Wh/km).
            if electric is not None and electric < 1000:
                record.update(consumption=round(electric / 10, 3), consumptionStandard="WLTP")
        else:
            if engine_power is not None:
                record["enginePowerKW" if hybrid_class else "powerKW"] = engine_power
            # The weighted PHEV combined values assume a laboratory charging
            # pattern. They are not the consumption for a petrol-only drive.
            consumption = number(row.get("49. WLTPCombinedFuelConsChargeSust")) if plugin else number(row.get("49. WLTPCombinedFuelCons"))
            if consumption is not None:
                record.update(consumption=consumption, consumptionStandard="WLTP charge sustaining" if plugin else "WLTP")
            # The PHEV electrical fields are weighted or charge-depleting
            # figures, neither a guarantee of pure-electric mode consumption.
        if pure_electric:
            add_rating(specs, "electricCombined", row.get("49. WLTPElecEnergyConsPureElectric"), "kWh/100 km", "WLTP combined", 1000, 0.1)
            add_rating(specs, "electricRange", row.get("49. WLTPElectricRangePureElectric"), "km", "WLTP electric", 2000)
            extra_rating(specs, "electric-city-range", "Electric range in city", row.get("49. WLTPElectricRangeCityPureElec"), "km", "WLTP city", 2000)
            for phase, column in [("Low", "WLTPLowElecEnergyConsPureElec"), ("Medium", "WLTPMediumElEnergyConsPureElec"), ("High", "WLTPHighElecEnergyConsPureElec"), ("Extra-high", "WLTPExtrHighElecEnConsPureElec")]:
                extra_rating(specs, "electric-" + phase.lower(), "Electricity · " + phase.lower() + " speed", row.get("49. " + column), "kWh/100 km", "WLTP " + phase.lower(), 1000, 0.1)
        elif plugin:
            add_rating(specs, "fuelCombined", row.get("49. WLTPWeightedCombinedFuelCons"), "L/100 km", "WLTP weighted", 100)
            add_rating(specs, "fuelDischarged", row.get("49. WLTPCombinedFuelConsChargeSust"), "L/100 km", "WLTP charge sustaining", 100)
            add_rating(specs, "electricCombined", row.get("49. WLTPElectEnergyConsExternCharg"), "kWh/100 km", "WLTP weighted", 1000, 0.1)
            if "electricCombined" not in specs:
                add_rating(specs, "electricCombined", row.get("49. ElectricEnergyConsWeightedComb"), "kWh/100 km", "NEDC weighted", 1000, 0.1)
            add_rating(specs, "electricRange", row.get("49. WLTPElectricRangeExternCharg"), "km", "WLTP electric", 2000)
            extra_rating(specs, "electric-city-range", "Electric range in city", row.get("49. WLTPElectricRangeCityExtCharg"), "km", "WLTP city", 2000)
            extra_rating(specs, "all-electric-range", "All-electric range", row.get("49. WLTPAllElectricRangeExternCharg"), "km", "WLTP all-electric", 2000)
            extra_rating(specs, "electric-charge-depleting", "Electricity with battery charge", row.get("49. WLTPCombElEnergyConsExtCharg"), "kWh/100 km", "WLTP charge depleting", 1000, 0.1)
            extra_rating(specs, "fuel-charge-depleting", "Fuel with battery charge", row.get("49. WLTPCombinedFuelConsChargeDepl"), "L/100 km", "WLTP charge depleting", 100)
            for phase, column in [("Low", "WLTPLowFuelConsChargeSust"), ("Medium", "WLTPMediumFuelConsChargeSust"), ("High", "WLTPHighFuelConsChargeSust"), ("Extra-high", "WLTPExtraHighFuelConsChargSust")]:
                extra_rating(specs, "fuel-discharged-" + phase.lower(), "Fuel, depleted battery · " + phase.lower() + " speed", row.get("49. " + column), "L/100 km", "WLTP charge sustaining", 100)
            for phase, column in [("Low", "WLTPLowElecEnergyConsExtCharg"), ("Medium", "WLTPMediumElEnergyConsExtCharg"), ("High", "WLTPHighElEnergyConsExtCharg"), ("Extra-high", "WLTPExtrHighElecEnConsExtCharg"), ("City", "WLTPCityElecEnergyConsExtCharg")]:
                extra_rating(specs, "electric-charged-" + phase.lower(), "Electricity · " + phase.lower(), row.get("49. " + column), "kWh/100 km", "WLTP charge depleting", 1000, 0.1)
        else:
            add_rating(specs, "fuelCombined", row.get("49. WLTPCombinedFuelCons"), "L/100 km", "WLTP combined", 100)
            for phase, column in [("Low", "WLTPLowFuelConsumption"), ("Medium", "WLTPMediumFuelConsumption"), ("High", "WLTPHighFuelConsumption"), ("Extra-high", "WLTPExtraHighFuelConsumption")]:
                extra_rating(specs, "fuel-" + phase.lower(), "Fuel · " + phase.lower() + " speed", row.get("49. " + column), "L/100 km", "WLTP " + phase.lower(), 100)
        if plugin:
            extra_rating(specs, "nedc-fuel-weighted", "Weighted fuel", row.get("49. WeightedCombinedFuelCons"), "L/100 km", "NEDC weighted", 100)
        if not pure_electric:
            for phase, column in [("City", "UrbanConditionsFuelConsumption"), ("Extra-urban", "ExtraUrbanConditionsFuelCons"), ("Combined", "CombinedFuelConsumption")]:
                extra_rating(specs, "nedc-fuel-" + phase.lower(), "Fuel · " + phase.lower(), row.get("49. " + column), "L/100 km", "NEDC " + phase.lower(), 100)
            if "fuelCombined" not in specs and not plugin:
                add_rating(specs, "fuelCombined", row.get("49. CombinedFuelConsumption"), "L/100 km", "NEDC combined", 100)
        if specs:
            specs["sourceURL"] = "https://www.astra.admin.ch/en/vehicle-data"
            record["specs"] = specs
        yield record


def manufacturer_supplements():
    # CUPRA distinguishes combustion-engine output from system output. These
    # two variant facts are from the manufacturer's technical publication,
    # not inferred by adding engine and electric-motor power together.
    source = "https://www.cupra.com/content/dam/public/cupra-website/cars/terramar/broachure/cupra-terramar-catalogue-jan-2025.pdf"
    # CUPRA publishes 19.7 kWh net for both PHEVs. Technical brochures also
    # display this as ~20 kWh net; 25.8 kWh is the separate gross capacity.
    capacity_source = "https://www.seat-cupra-mediacenter.com/content/dam/seat-media-center/models-all-brands/cupra-models/cupra-terramar/technical-data/Techninal-Data-CUPRA-Terramar.pdf"
    for ps, system, engine, fuel_lo, fuel_hi, electric_lo, electric_hi, range_lo, range_hi in [
        (204, 150, 110, 5.8, 6.3, 17.6, 19.0, 113, 123),
        (272, 200, 130, 5.7, 6.1, 17.5, 19.0, 112, 121),
    ]:
        def measure(lo, hi, unit, standard):
            return {"min": lo, "max": hi, "unit": unit, "standard": standard}
        yield {"make": "CUPRA", "model": "Terramar", "variant": f"1.5 e-HYBRID {ps} PS · 6-speed DSG",
               "energy": "Plug-in hybrid", "enginePowerKW": engine, "systemPowerKW": system,
               "sourceURL": source,
               "specs": {
                   "fuelCombined": measure(0.4, 0.5, "L/100 km", "WLTP weighted"),
                   "fuelDischarged": measure(fuel_lo, fuel_hi, "L/100 km", "WLTP charge sustaining"),
                   "electricCombined": measure(electric_lo, electric_hi, "kWh/100 km", "WLTP combined"),
                   "electricRange": measure(range_lo, range_hi, "km", "WLTP electric"),
                   "batteryKWh": measure(25.8, 25.8, "kWh", "Gross capacity"),
                   "fuelTankLitres": measure(45, 45, "L", "Fuel tank capacity"),
                   "usableBatteryKWh": measure(19.7, 19.7, "kWh", "Net usable capacity"),
                   "capacitySourceURL": capacity_source,
                   "engineCC": measure(1498, 1498, "cc", "Engine displacement"),
                   "gearbox": "6-speed DSG", "drive": "Front-wheel drive", "sourceURL": source,
               }}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--autos", type=Path, default=Path("/tmp/carloss-astra-auto.csv"))
    parser.add_argument("--emissions", type=Path, default=Path("/tmp/carloss-astra-emissions.csv"))
    parser.add_argument("--edata", type=Path, default=Path("/tmp/carloss-astra-edatenblatt.csv"))
    parser.add_argument("--updated-at", default="2026-11-04", help="Latest source snapshot date, YYYY-MM-DD")
    parser.add_argument("--output", type=Path, default=ROOT / "ios/CarlossTracker/Resources/VehicleCatalog.json")
    args = parser.parse_args()
    groups = {}
    for record in chain(tas_records(args.autos, args.emissions), edata_records(args.edata)):
        # Deduplicate equivalent approved specifications, excluding VINs,
        # individual vehicle dates, and cryptic CoC codes from the app payload.
        identity = {k: v for k, v in record.items() if k not in {"consumption", "electricConsumption", "consumptionStandard", "specs"}}
        key = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if key not in groups:
            groups[key] = {"record": identity, "consumption": set(), "standard": set(), "specs": {}}
        groups[key]["consumption"].add(record.get("consumption"))
        groups[key]["standard"].add(record.get("consumptionStandard"))
        merge_specs(groups[key]["specs"], record.get("specs", {}))
    catalog = []
    for key, group in groups.items():
        record = group["record"]
        # One display identity can cover different years, tyres or trims. Keep
        # a consumption default only when ALL source rows agree; otherwise
        # the owner confirms their own number during setup.
        consumption = unique_number(group["consumption"])
        if consumption is not None and len(group["standard"]) == 1:
            record.update(consumption=consumption, consumptionStandard=next(iter(group["standard"])))
        if group["specs"]:
            record["specs"] = group["specs"]
        if record["make"].upper() == "CUPRA" and "TERRAMAR" in record["model"].upper() and record["energy"] == "Plug-in hybrid":
            # Replaced below by explicit manufacturer-named system variants.
            continue
        record["id"] = "astra-" + hashlib.sha256(key.encode()).hexdigest()[:16]
        catalog.append(record)
    for record in manufacturer_supplements():
        record["id"] = "cupra-terramar-phev-" + str(record["systemPowerKW"])
        catalog.append(record)
    vehicles = sorted(catalog, key=lambda r: (r["make"].casefold(), r["model"].casefold(), r["energy"], r.get("systemPowerKW", r.get("powerKW", r.get("enginePowerKW", 0)))))
    payload = {"source": "ASTRA / FEDRO", "sourceURL": "https://www.astra.admin.ch/en/vehicle-data",
               "updatedAt": args.updated_at, "vehicles": vehicles}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(json.dumps({"vehicles": len(vehicles), "makes": len({r['make'] for r in vehicles}),
                      "withConsumption": sum('consumption' in r for r in vehicles),
                      "withElectricConsumption": sum('electricConsumption' in r for r in vehicles),
                      "withFuelRatings": sum('fuelCombined' in r.get('specs', {}) for r in vehicles),
                      "withElectricRatings": sum('electricCombined' in r.get('specs', {}) for r in vehicles),
                      "withAnyConsumptionRatings": sum(any(k in r.get('specs', {}) for k in ['fuelCombined', 'electricCombined']) for r in vehicles),
                      "withFuelTankCapacity": sum('fuelTankLitres' in r.get('specs', {}) for r in vehicles),
                      "withUsableBatteryCapacity": sum('usableBatteryKWh' in r.get('specs', {}) for r in vehicles),
                      "bytes": args.output.stat().st_size}, indent=2))


if __name__ == "__main__":
    main()
