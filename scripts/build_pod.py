#!/usr/bin/env python3
"""
build_pod.py -- idempotent NetBox generator for the reference GPU pod.

Reads spec/pod.yaml (a description of the desired end state) and reconciles
NetBox against it. Safe to run repeatedly: objects that already exist are left
alone, only missing ones are created. It does not track what previous runs did
-- it reads current state fresh every time, so an interrupted run is fixed by
simply running it again.

Usage:
    python scripts/build_pod.py --dry-run     # report only, change nothing
    python scripts/build_pod.py               # reconcile

Slice 1 of 4: racks. Device types, devices and power come next.
"""

import argparse
import os
import sys

import pynetbox
import requests
import yaml

from netbox_client import REPO_ROOT, ConfigError, connect


class Planned:
    """Stands in for an object a dry run would create but has not.

    Without this, a dry run cannot distinguish "this rack is blocked" from
    "I just haven't made its role yet because I'm not writing anything." The
    first is a problem; the second is an artefact of previewing. Carrying a
    placeholder forward lets dependent steps report honestly.

    `id` is None so that callers building a payload eagerly do not blow up;
    the payload is never sent while anything in the chain is merely planned.
    """

    id = None

    def __init__(self, label):
        self.label = label


class Runner:
    """Wraps the NetBox API with get-or-create semantics and a run tally."""

    def __init__(self, nb, dry_run):
        self.nb = nb
        self.dry_run = dry_run
        self.created = 0
        self.existing = 0
        self.planned = 0
        self.skipped = 0

    def ensure(self, endpoint, lookup, payload, label, depends_on=()):
        """Return the object described by `lookup`, creating it if absent.

        `lookup` is how we FIND an existing object -- it must be specific
        enough to match at most one. `payload` is what we send to create it.
        They differ because NetBox filters and NetBox write fields are not the
        same shape (e.g. filter on site_id, create with site). Conflating them
        is the usual cause of a get-or-create that silently makes duplicates.

        `depends_on` lists parent objects this one references. If any is only
        planned, we cannot query or create yet -- but we know it WOULD be
        created, so say so rather than reporting a skip.
        """
        pending = [d.label for d in depends_on if isinstance(d, Planned)]
        if pending:
            self.planned += 1
            print(f"  +  {label}  (would create, after {', '.join(pending)})")
            return Planned(label)

        if any(d is None for d in depends_on):
            self.skipped += 1
            print(f"  .  {label}  (skipped: prerequisite missing)")
            return None

        obj = endpoint.get(**lookup)
        if obj is not None:
            self.existing += 1
            print(f"  =  {label}")
            return obj

        if self.dry_run:
            self.planned += 1
            print(f"  +  {label}  (would create)")
            return Planned(label)

        obj = endpoint.create(payload)
        self.created += 1
        print(f"  +  {label}")
        return obj

    def summary(self):
        print()
        print(f"existing: {self.existing}")
        if self.dry_run:
            print(f"would create: {self.planned}")
        else:
            print(f"created: {self.created}")
        if self.skipped:
            print(f"skipped: {self.skipped}")


def expand(pattern):
    """Expand a NetBox-style name range into concrete names.

    "port[1-64]" -> ["port1", ..., "port64"];  "bmc" -> ["bmc"]

    The NetBox UI expands these ranges when you bulk-add components, but the
    REST API does not -- it takes literal names. Keeping the same syntax in
    the spec means what you write here matches what you would type in the
    form, and this function bridges the difference.
    """
    if "[" not in pattern:
        return [pattern]

    head, rest = pattern.split("[", 1)
    body, tail = rest.split("]", 1)
    start, end = (int(x) for x in body.split("-"))
    return [f"{head}{i}{tail}" for i in range(start, end + 1)]


def ensure_components(run, endpoint, device_type, wanted, kind):
    """Create any of `wanted` that this device type does not already have.

    Fetches existing templates once and filters client-side on device_type.id
    rather than passing a filter argument, because the filter parameter name
    for template endpoints has changed between NetBox versions and this is
    both version-proof and one request instead of many.
    """
    # Check for the sentinel BEFORE touching any attribute a real object has.
    # Planned carries only .label and .id, by design -- it is a marker, not a
    # stand-in that pretends to be a device type.
    if isinstance(device_type, Planned):
        run.planned += len(wanted)
        print(f"  +  {device_type.label}: {len(wanted)} {kind}  (would create)")
        return

    label = f"{device_type.model}: {len(wanted)} {kind}"

    existing = {
        t.name for t in endpoint.all()
        if t.device_type and t.device_type.id == device_type.id
    }
    missing = [w for w in wanted if w["name"] not in existing]

    if not missing:
        run.existing += len(wanted)
        print(f"  =  {label}")
        return

    if run.dry_run:
        run.planned += len(missing)
        print(f"  +  {device_type.model}: {len(missing)} {kind}  (would create)")
        return

    # One bulk POST rather than a request per component -- 64 interfaces on a
    # QM9700 is otherwise 64 round trips.
    endpoint.create([{**m, "device_type": device_type.id} for m in missing])
    run.created += len(missing)
    run.existing += len(wanted) - len(missing)
    print(f"  +  {device_type.model}: {len(missing)} {kind} created")


def build_device_types(run, spec):
    """Device types plus their interface and power-port templates."""

    print("\ndevice types")
    for dt_spec in spec.get("device_types", []):
        manufacturer = run.ensure(
            run.nb.dcim.manufacturers,
            {"slug": dt_spec["manufacturer_slug"]},
            {
                "name": dt_spec["manufacturer"],
                "slug": dt_spec["manufacturer_slug"],
            },
            dt_spec["manufacturer"],
        )

        payload = {
            "manufacturer": manufacturer.id,
            "model": dt_spec["model"],
            "slug": dt_spec["slug"],
            "u_height": dt_spec["u_height"],
            "is_full_depth": dt_spec.get("full_depth", True),
            "airflow": dt_spec["airflow"],
            "description": dt_spec.get("description", ""),
        }
        if "weight" in dt_spec:
            payload["weight"] = dt_spec["weight"]
            payload["weight_unit"] = dt_spec["weight_unit"]

        device_type = run.ensure(
            run.nb.dcim.device_types,
            {"slug": dt_spec["slug"]},
            payload,
            dt_spec["model"],
            depends_on=(manufacturer,),
        )

        if device_type is None:
            continue

        power_ports = []
        for pp in dt_spec.get("power_ports", []):
            for name in expand(pp["name"]):
                power_ports.append({
                    "name": name,
                    "type": pp["type"],
                    "maximum_draw": pp["maximum_draw"],
                    "allocated_draw": pp["allocated_draw"],
                })
        if power_ports:
            ensure_components(
                run, run.nb.dcim.power_port_templates,
                device_type, power_ports, "power ports",
            )

        interfaces = []
        for iface in dt_spec.get("interfaces", []):
            for name in expand(iface["name"]):
                interfaces.append({
                    "name": name,
                    "type": iface["type"],
                    "mgmt_only": iface.get("mgmt_only", False),
                })
        if interfaces:
            ensure_components(
                run, run.nb.dcim.interface_templates,
                device_type, interfaces, "interfaces",
            )


def build_racks(run, spec):
    """Region -> Site -> Location -> RackType + Roles -> 12 racks."""

    print("\nregion")
    r = spec["region"]
    region = run.ensure(
        run.nb.dcim.regions,
        {"slug": r["slug"]},
        {"name": r["name"], "slug": r["slug"]},
        r["name"],
    )

    print("\nsite")
    s = spec["site"]
    site = run.ensure(
        run.nb.dcim.sites,
        {"slug": s["slug"]},
        {
            "name": s["name"],
            "slug": s["slug"],
            "status": s["status"],
            "description": s.get("description", ""),
            # In dry-run the region may not exist yet; omit rather than crash.
            **({"region": region.id} if region else {}),
        },
        s["name"],
    )

    print("\nlocation")
    loc_spec = spec["location"]
    location = run.ensure(
        run.nb.dcim.locations,
        {"slug": loc_spec["slug"], "site_id": site.id},
        {
            "name": loc_spec["name"],
            "slug": loc_spec["slug"],
            "site": site.id,
            "status": loc_spec["status"],
        },
        loc_spec["name"],
        depends_on=(site,),
    )

    print("\nmanufacturer")
    rt = spec["rack_type"]
    manufacturer = run.ensure(
        run.nb.dcim.manufacturers,
        {"slug": rt["manufacturer_slug"]},
        {"name": rt["manufacturer"], "slug": rt["manufacturer_slug"]},
        rt["manufacturer"],
    )

    print("\nrack type")
    rack_type = run.ensure(
        run.nb.dcim.rack_types,
        {"slug": rt["slug"]},
        {
            "manufacturer": manufacturer.id,
            "model": rt["model"],
            "slug": rt["slug"],
            "form_factor": rt["form_factor"],
            "width": rt["width"],
            "u_height": rt["u_height"],
            "starting_unit": rt["starting_unit"],
            "outer_width": rt["outer_width"],
            "outer_depth": rt["outer_depth"],
            "outer_unit": rt["outer_unit"],
            "max_weight": rt["max_weight"],
            "weight_unit": rt["weight_unit"],
        },
        rt["model"],
        depends_on=(manufacturer,),
    )

    print("\nrack roles")
    roles = {}
    for role_spec in spec["rack_roles"]:
        role = run.ensure(
            run.nb.dcim.rack_roles,
            {"slug": role_spec["slug"]},
            {
                "name": role_spec["name"],
                "slug": role_spec["slug"],
                "color": role_spec["color"],
            },
            role_spec["name"],
        )
        roles[role_spec["slug"]] = role

    print("\nracks")
    for rack_spec in spec["racks"]:
        name = rack_spec["name"]
        role = roles.get(rack_spec["role"])

        run.ensure(
            run.nb.dcim.racks,
            {"name": name, "site_id": site.id},
            {
                "name": name,
                "site": site.id,
                "location": location.id,
                "rack_type": rack_type.id,
                "role": role.id,
                "status": "active",
            },
            name,
            depends_on=(site, location, rack_type, role),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )
    parser.add_argument(
        "--spec",
        default=os.path.join(REPO_ROOT, "spec", "pod.yaml"),
        help="path to the pod spec (default: spec/pod.yaml)",
    )
    args = parser.parse_args()

    with open(args.spec) as fh:
        spec = yaml.safe_load(fh)

    try:
        nb, info = connect()
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    mode = "DRY RUN -- no changes will be written" if args.dry_run else "APPLY"
    print(f"{info['url']}  [{mode}]")

    run = Runner(nb, args.dry_run)
    try:
        build_racks(run, spec)
        build_device_types(run, spec)
    except requests.exceptions.ConnectionError:
        print(f"\nFAIL: lost connection to {info['url']}", file=sys.stderr)
        return 1
    except pynetbox.RequestError as exc:
        # Print the tally so far -- a partial run is recoverable, and knowing
        # how far it got is more useful than just the traceback.
        run.summary()
        print(f"\nFAIL: NetBox rejected a write: {exc}", file=sys.stderr)
        return 1

    run.summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
