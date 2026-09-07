#!/usr/bin/env python3
"""
Print what NetBox 4.7's cooling model actually looks like on THIS instance.

Phase 2 models liquid-cooled racks against objects that did not exist when the
rest of this repo was written. Documentation describes them; only the running
instance can say what its API will accept. Same rule as show_choices.py: read
the values off the live server rather than guessing and debugging a 400 in the
middle of a build.

For each cooling endpoint this prints the writable field list from an HTTP
OPTIONS request -- name, type, required, and any choice values -- which is the
authoritative answer to "what goes in the spec file".

It then diffs each cooling model against the power model it is said to mirror.
The release notes describe cooling as the power model's twin; the fields are
where that claim either holds or stops holding, and the difference decides
whether a cooling topology can be walked the way the power chain was.

Usage:
    python scripts/show_cooling.py
    python scripts/show_cooling.py --raw     # full JSON for one endpoint
"""

import argparse
import json
import os
import sys

import requests

from netbox_client import ConfigError, connect

# Endpoints introduced by the 4.7 cooling model, plus the device-type
# templates that stamp intakes and outflows onto devices at creation time.
COOLING_ENDPOINTS = [
    "cooling-sources",
    "cooling-feeds",
    "cooling-intakes",
    "cooling-outflows",
    "cooling-intake-templates",
    "cooling-outflow-templates",
]

# Each cooling model against the power model it is described as mirroring.
# Where the analogy breaks is where phase 1's technique stops transferring.
ANALOGUES = [
    ("power-panels", "cooling-sources"),
    ("power-feeds", "cooling-feeds"),
    ("power-ports", "cooling-intakes"),
    ("power-outlets", "cooling-outflows"),
    ("power-port-templates", "cooling-intake-templates"),
    ("power-outlet-templates", "cooling-outflow-templates"),
]

# Fields that make the power model *computable* rather than merely descriptive:
# the demand values NetBox sums, and the cable path it sums them along. Called
# out by name because their absence is easy to miss in a long field list, and
# it is exactly what decides whether a utilisation figure can be derived.
LOAD_BEARING = {
    "allocated_draw", "maximum_draw",      # demand
    "cable", "cable_end", "link_peers", "link_peers_type",
    "connected_endpoints", "connected_endpoints_type",
    "connected_endpoints_reachable",       # the path demand travels
    "power_port",                          # outlet -> parent port
}

# The cooling model keeps some power concepts under a different name. A plain
# set difference reports those as missing, which is a false alarm dressed
# identically to a real one -- the same failure mode as the CHECK/PASS verdict
# column in S6. Renames are resolved here so that what the report flags is
# only ever a capability that genuinely has no counterpart.
EQUIVALENTS = {
    "power_port": "cooling_intake",        # downstream component -> its parent
}

# Cooling-related fields added to models that already existed. These are the
# ones a spec has to set, and they are easy to miss because the objects are
# otherwise familiar.
EXISTING_MODEL_FIELDS = {
    "racks": ["cooling_capability", "cooling_capacity"],
    "rack-types": ["cooling_capability", "cooling_capacity"],
    "devices": ["cooling_method"],
    "device-types": ["cooling_method"],
    "module-types": ["cooling_method"],
}


def authorized_session(nb, info):
    """A requests session carrying whichever auth scheme this token needs.

    connect() attaches Bearer auth to the session itself, but the classic
    Token scheme is applied per-request by pynetbox and never reaches the
    session -- so set it here for the raw OPTIONS calls below.
    """
    session = nb.http_session
    if session.auth is None and "Authorization" not in session.headers:
        credential = os.environ.get("NETBOX_TOKEN", "").strip()
        session.headers["Authorization"] = f"Token {credential}"
    return session


def options(session, base, path):
    """OPTIONS an endpoint; return its JSON, or None if it does not exist."""
    response = session.options(
        f"{base}{path}", headers={"Accept": "application/json"}
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def post_fields(doc):
    """The writable field map from an OPTIONS response, or {}.

    DRF puts it at actions.POST. A missing actions block means the credential
    cannot write to this endpoint -- worth distinguishing from an endpoint
    that does not exist at all.
    """
    return (doc or {}).get("actions", {}).get("POST", {})


def describe(name, spec):
    """One line per field: name, type, required marker."""
    kind = spec.get("type", "?")
    required = "required" if spec.get("required") else ""
    label = spec.get("label", "")
    return f"    {name:<24} {kind:<14} {required:<9} {label}"


def choice_values(spec):
    """Choice values for a field, if it has any."""
    return [c.get("value") for c in spec.get("choices", []) if "value" in c]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--raw",
        metavar="ENDPOINT",
        help="dump the full OPTIONS JSON for one endpoint, e.g. cooling-feeds",
    )
    args = parser.parse_args()

    try:
        nb, info = connect()
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    base = info["url"].rstrip("/") + "/api/dcim/"
    session = authorized_session(nb, info)

    version = nb.status()["netbox-version"]
    print(f"{info['url']}  (NetBox {version})\n")

    if args.raw:
        doc = options(session, base, f"{args.raw}/")
        if doc is None:
            print(f"FAIL: no such endpoint: {args.raw}", file=sys.stderr)
            return 1
        print(json.dumps(doc, indent=2))
        return 0

    # 1. Which cooling endpoints this instance actually serves. An endpoint
    #    that 404s here is one the spec must not reference, whatever the
    #    release notes say -- 4.7.0 and 4.7.x may differ.
    print("=== cooling endpoints ===\n")
    present = {}
    for path in COOLING_ENDPOINTS:
        doc = options(session, base, f"{path}/")
        if doc is None:
            print(f"  ABSENT   {path}")
            continue
        fields = post_fields(doc)
        if not fields:
            print(f"  {path}  -- present, but no writable schema returned "
                  f"(read-only credential?)")
            continue
        present[path] = fields
        print(f"  OK       {path}  ({len(fields)} writable fields)")

    if not present:
        print("\nNo cooling endpoints found. Check the NetBox version above "
              "-- the cooling model arrives in 4.7.")
        return 1

    # 2. The field list per endpoint. This is what the spec has to produce,
    #    and it settles the questions the docs leave open -- notably whether
    #    a cooling intake references a cooling FEED directly, or only an
    #    upstream outflow on another device.
    for path, fields in present.items():
        print(f"\n=== {path} ===\n")
        for name, spec in sorted(fields.items()):
            print(describe(name, spec))
            values = choice_values(spec)
            if values:
                print(f"        choices: {', '.join(str(v) for v in values)}")
            related = spec.get("related_model") or spec.get("relatedModel")
            if related:
                print(f"        -> {related}")

    # 3. Cooling fields grafted onto models that already existed. Missing
    #    these is how a rack ends up modelled as liquid-cooled everywhere
    #    except in the one field a report would filter on.
    print("\n=== cooling fields on existing models ===")
    for path, wanted in EXISTING_MODEL_FIELDS.items():
        doc = options(session, base, f"{path}/")
        fields = post_fields(doc)
        print(f"\n  {path}")
        if not fields:
            print("    (no writable schema returned)")
            continue
        for name in wanted:
            spec = fields.get(name)
            if spec is None:
                print(f"    {name:<24} ABSENT on this version")
                continue
            print(describe(name, spec))
            values = choice_values(spec)
            if values:
                print(f"        choices: {', '.join(str(v) for v in values)}")

    # 4. How far the power analogy actually reaches.
    #
    # The interesting column is "only on the power model". Power is computable
    # because a port declares a draw and a cable carries it upstream; if the
    # cooling equivalents carry neither, then a cooling utilisation figure
    # cannot be walked out of the model the way the N-1 verdict was in S6 --
    # it has to be derived from the power model and stated as such.
    print("\n=== how far the power analogy reaches ===")
    for power_path, cooling_path in ANALOGUES:
        power = set(post_fields(options(session, base, f"{power_path}/")))
        cooling = set(post_fields(options(session, base, f"{cooling_path}/")))
        if not power or not cooling:
            print(f"\n  {power_path} vs {cooling_path}: schema unavailable")
            continue

        print(f"\n  {power_path}  vs  {cooling_path}")
        only_power = sorted(power - cooling)
        only_cooling = sorted(cooling - power)
        renamed = {
            f: EQUIVALENTS[f] for f in only_power
            if EQUIVALENTS.get(f) in cooling
        }
        missing_load_bearing = [
            f for f in only_power if f in LOAD_BEARING and f not in renamed
        ]

        print(f"    shared:            {len(power & cooling)} fields")
        print(f"    only on power:     {', '.join(only_power) or '-'}")
        print(f"    only on cooling:   {', '.join(only_cooling) or '-'}")
        if renamed:
            print("    renamed, not missing: "
                  + ", ".join(f"{p} -> {c}" for p, c in sorted(renamed.items())))
        if missing_load_bearing:
            print(f"    NO COOLING EQUIVALENT for: "
                  f"{', '.join(missing_load_bearing)}")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.exceptions.ConnectionError as exc:
        print(f"\nFAIL: could not reach NetBox: {exc}", file=sys.stderr)
        sys.exit(1)
