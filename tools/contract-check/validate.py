"""Development-only v1 oracle: structural, semantic and raw-byte validation."""

import argparse
import copy
import json
import math
from datetime import datetime, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/v1"
MAX_BYTES = 4096
DAY = 86400
WEEK = 7 * DAY
LABELS = ("M", "T", "W", "Th", "F", "Sa", "Su")
SCHEMAS = {}
for _kind in ("usage", "health", "error"):
    _schema = json.loads((CONTRACT / f"{_kind}.schema.json").read_text())
    Draft202012Validator.check_schema(_schema)
    SCHEMAS[_kind] = Draft202012Validator(_schema)


class Invalid(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code="semantics"):
    if not condition:
        raise Invalid(code)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "malformed")
        result[key] = value
    return result


def finite_number(raw):
    value = float(raw)
    require(math.isfinite(value), "malformed")
    return value


def bad_constant(_):
    raise Invalid("malformed")


def reset_text(epoch, zone):
    value = datetime.fromtimestamp(epoch, zone).strftime("%Y-%m-%d %H:%M %z")
    return value[:-2] + ":" + value[-2:]


def freshness(value):
    observed = value["observed_at"]
    if observed is None:
        require((value["updated_at"] is None) == (value["source_error"] is None))
        return "unavailable", "no_observation", None
    age = max(0, value["as_of"] - observed)
    if value["as_of"] + 5 < max(observed, value["updated_at"]):
        return "stale", "clock_error", age
    if value["source_error"] is not None:
        return "stale", "source_error", age
    if value["as_of"] >= value["reset_at"]:
        return "stale", "reset_due", age
    if age >= value["stale_after_seconds"]:
        return "stale", "too_old", age
    return "ok", None, age


def calendar_bounds(end, zone):
    start = end-WEEK
    out = []
    while start < end:
        tomorrow = datetime.fromtimestamp(start, zone).date()+timedelta(days=1)
        stop = min(end, int(datetime.combine(tomorrow, time(), zone).timestamp()))
        require(stop>start and len(out)<9)
        out.append((start, stop)); start=stop
    require(7<=len(out)<=9)
    return out


def semantics(value):
    expiry = value.get("resets_expire_at")
    if expiry is not None:
        require(value["version"] == 2 and value["resets_available"] is not None and value["resets_available"] > 0)
        require(value["observed_at"] is not None and expiry > value["observed_at"])
    try:
        zone = ZoneInfo(value["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise Invalid("semantics") from None
    cycle = value["cycle"]
    observed = value["observed_at"]
    if observed is None:
        require(all(value[k] is None for k in (
            "scope", "remaining_percent", "reset_at", "reset_local", "resets_available"
        )))
        require(cycle == {"start_at": None, "end_at": None, "state": "unknown"})
        if value['version']==2: require(value['days']==[])
        else: require(all(d == {"start_at": None, "label": None, "used_delta_pp": None,
                          "coverage": "unknown"} for d in value["days"]))
    else:
        require(all(value[k] is not None for k in (
            "scope", "remaining_percent", "reset_at", "reset_local", "updated_at"
        )))
        require(cycle["start_at"] is not None and cycle["end_at"] is not None)
        require(cycle["state"] != "unknown")
        require(cycle["end_at"] == value["reset_at"])
        require(cycle["end_at"] - cycle["start_at"] == WEEK)
        require(cycle["start_at"] - 5 <= observed < cycle["end_at"])
        require(value["updated_at"] >= observed)
        require(value["reset_local"] == reset_text(value["reset_at"], zone))
        # A backwards wall clock must not erase facts observed before the jump.
        effective_now = max(value["as_of"], observed, value["updated_at"])
        bounds = calendar_bounds(cycle['end_at'], zone) if value['version']==2 else [(cycle['start_at']+i*DAY,cycle['start_at']+(i+1)*DAY) for i in range(7)]
        require(len(bounds)==len(value['days']))
        for i, day in enumerate(value["days"]):
            start, end = bounds[i]
            if value["version"]==2: require(day["end_at"]==end)
            require(day["start_at"] == start)
            require(day["label"] == LABELS[datetime.fromtimestamp(start, zone).weekday()])
            coverage, delta = day["coverage"], day["used_delta_pp"]
            if start > effective_now:
                require(coverage == "future" and delta == 0)
            elif cycle["state"] == "ambiguous":
                require(coverage == "unknown" and delta is None)
            else:
                require(coverage != "future")
                require((coverage == "unknown") == (delta is None))
                require(coverage != "complete" or end <= effective_now)
        require(sum(d["used_delta_pp"] or 0 for d in value["days"]) <= 100)
    require((value["status"], value["reason"], value["age_seconds"]) == freshness(value))


def validate(raw, kind="usage", version=1):
    require(len(raw) <= (MAX_BYTES if kind == "usage" else 256), "oversized")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs,
                           parse_constant=bad_constant, parse_float=finite_number)
    except (ValueError, RecursionError) as error:
        if isinstance(error, Invalid):
            raise
        raise Invalid("malformed") from None
    if isinstance(value, dict) and "version" in value and (
        type(value["version"]) not in (int, float) or value["version"] != version
    ):
        raise Invalid("version")
    validator=SCHEMAS[kind] if version==1 else Draft202012Validator(json.loads((ROOT/f"contracts/v2/{kind}.schema.json").read_text()))
    require(validator.is_valid(value), "schema")
    if kind == "usage":
        semantics(value)
    return value


def validate_history(raw, persisted=False):
    require(len(raw)<=65536,'oversized')
    try:
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=unique_pairs,parse_constant=bad_constant,parse_float=finite_number)
    except (ValueError,RecursionError):
        raise Invalid('malformed') from None
    require(isinstance(value,dict),'schema')
    checked=copy.deepcopy(value)
    if persisted:
        require('as_of' not in value and value.get('updated_at') is not None)
        checked['as_of']=value['updated_at']
    schema=json.loads((ROOT/'contracts/v2/history.schema.json').read_text())
    require(Draft202012Validator(schema).is_valid(checked),'schema')
    if checked['updated_at'] is None:require(checked['scope'] is None and checked['cycles']==[])
    previous=0
    for cycle in checked['cycles']:
        validate(encode(cycle),version=2)
        require(cycle['scope']==checked['scope'] and cycle['observed_at'] is not None)
        require(previous<cycle['reset_at'] and cycle['updated_at']<=checked['updated_at'])
        if persisted:require(cycle['updated_at']==cycle['as_of'])
        else:require(cycle['as_of']==checked['as_of'])
        require(cycle['reset_at']>checked['as_of']-checked['retention_seconds'])
        previous=cycle['reset_at']
    return value


def encode(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False).encode()


def validate_snapshot(raw, version=1):
    """A persisted publication always has a native publication timestamp."""
    value = validate(raw, version=version)
    require(value["updated_at"] is not None and value["as_of"] == value["updated_at"])
    return value


def project(value, now):
    """Reference API projection; input must already pass validate at its as_of."""
    result = copy.deepcopy(value)
    result["as_of"] = now
    effective_now = max(now, result["observed_at"] or now, result["updated_at"] or now)
    for day in result["days"]:
        if day["coverage"] == "future" and day["start_at"] <= effective_now:
            day.update(coverage="unknown", used_delta_pp=None)
    result["status"], result["reason"], result["age_seconds"] = freshness(result)
    validate(encode(result),version=result["version"])
    return result


def check_fixtures(version=1):
    CONTRACT=ROOT/f"contracts/v{version}"
    manifest = json.loads((CONTRACT / "fixtures/manifest.json").read_text())
    sizes, accepted, rejected = [], 0, 0
    listed = {item["file"] for item in manifest}
    actual = {p.name for p in (CONTRACT / "fixtures").iterdir() if p.name != "manifest.json"}
    require(len(listed) == len(manifest) and listed == actual, "fixture_inventory")
    for item in manifest:
        raw = (CONTRACT / "fixtures" / item["file"]).read_bytes()
        try:
            validate(raw, item.get("kind", "usage"),version=version)
        except Invalid as error:
            if error.code != item["expect"]:
                raise AssertionError(f'{item["file"]}: expected {item["expect"]}, got {error.code}')
            rejected += 1
        else:
            require(item["expect"] == "valid", "unexpected_acceptance")
            accepted += 1
            if item.get("kind", "usage") == "usage":
                sizes.append(len(raw))
    print(json.dumps({"accepted": accepted, "rejected": rejected,
                      "largest_usage_bytes": max(sizes), "limit_bytes": MAX_BYTES}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path)
    parser.add_argument("--version", type=int, choices=[1,2], default=1)
    parser.add_argument("--kind", choices=SCHEMAS, default="usage")
    args = parser.parse_args()
    if args.file:
        with args.file.open("rb") as stream:
            validate(stream.read(MAX_BYTES + 1), args.kind, version=args.version)
        print("valid")
    else:
        check_fixtures(args.version)
