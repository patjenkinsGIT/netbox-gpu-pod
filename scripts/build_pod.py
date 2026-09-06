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
import re
import sys

import pynetbox
import requests
import yaml

from netbox_client import REPO_ROOT, ConfigError, connect


def scalar_diff(current, payload):
    """Declared fields whose stored value differs, excluding foreign keys.

    Foreign keys are skipped deliberately: comparing them means unwrapping
    nested records and reasoning about ids that do not drift in practice,
    and getting it wrong would make the reconciler rewrite relationships on
    every run. Scalars -- u_height, position, description, status -- are
    where spec drift actually shows up.
    """
    diff = {}
    for field, declared in payload.items():
        value = getattr(current, field, None)
        if hasattr(value, "id"):
            continue
        if hasattr(value, "value"):
            value = value.value
        # NetBox returns some numerics as strings or Decimals; compare
        # numerically first so 8 and "8.00" do not read as drift forever.
        try:
            if float(value) == float(declared):
                continue
        except (TypeError, ValueError):
            pass
        if value != declared:
            diff[field] = declared
    return diff


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
        self.updated = 0
        self.planned_updates = 0

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
            # Existence is not convergence -- reconcile declared scalar fields
            # too, or a device type whose u_height changed in the spec stays
            # wrong forever while the run reports success.
            diff = scalar_diff(obj, payload)
            if diff:
                summary = ", ".join(f"{k}={v!r}" for k, v in diff.items())
                if self.dry_run:
                    self.planned_updates += 1
                    print(f"  ~  {label}: {summary}  (would update)")
                else:
                    obj.update(diff)
                    self.updated += 1
                    print(f"  ~  {label}: {summary}")
            else:
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
            print(f"would update: {self.planned_updates}")
        else:
            print(f"created: {self.created}")
            print(f"updated: {self.updated}")
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

    by_name = {
        t.name: t for t in endpoint.all()
        if t.device_type and t.device_type.id == device_type.id
    }
    missing = [w for w in wanted if w["name"] not in by_name]

    # Existence is not convergence. A template can be present with the wrong
    # field values, and a create-only generator reports success while the model
    # is wrong -- which is how maximum_draw=34500 survived on the rPDU input
    # port and zeroed 33 racks' utilisation behind healthy cables.
    drifted = []
    for w in wanted:
        current = by_name.get(w["name"])
        if current is None:
            continue
        diff = {
            field: value for field, value in w.items()
            if field != "name" and field_value(current, field) != value
        }
        if diff:
            drifted.append((current, diff))

    if missing and not run.dry_run:
        # One bulk POST rather than a request per component -- 64 interfaces on
        # a QM9700 is otherwise 64 round trips.
        endpoint.create([{**m, "device_type": device_type.id} for m in missing])
        run.created += len(missing)
        print(f"  +  {device_type.model}: {len(missing)} {kind} created")
    elif missing:
        run.planned += len(missing)
        print(f"  +  {device_type.model}: {len(missing)} {kind}  (would create)")

    for current, diff in drifted:
        summary = ", ".join(f"{k}={v!r}" for k, v in diff.items())
        if run.dry_run:
            run.planned_updates += 1
            print(f"  ~  {device_type.model}/{current.name}: {summary}  (would update)")
        else:
            current.update(diff)
            run.updated += 1
            print(f"  ~  {device_type.model}/{current.name}: {summary}")

    unchanged = len(wanted) - len(missing) - len(drifted)
    run.existing += unchanged
    if unchanged and not missing and not drifted:
        print(f"  =  {label}")


def ensure_outlets(run, device_type, wanted):
    """Create power outlet templates, resolving each one's parent power port.

    Outlets reference their power port by id. Without that link NetBox has no
    path from a plugged-in device up to the feed, and utilisation silently
    reads zero -- the exact failure that cost an hour in S3.
    """
    kind = "power outlets"

    if isinstance(device_type, Planned):
        run.planned += len(wanted)
        print(f"  +  {device_type.label}: {len(wanted)} {kind}  (would create)")
        return

    ports = {
        p.name: p.id for p in run.nb.dcim.power_port_templates.all()
        if p.device_type and p.device_type.id == device_type.id
    }
    existing = {
        o.name for o in run.nb.dcim.power_outlet_templates.all()
        if o.device_type and o.device_type.id == device_type.id
    }
    missing = [w for w in wanted if w["name"] not in existing]

    if not missing:
        run.existing += len(wanted)
        print(f"  =  {device_type.model}: {len(wanted)} {kind}")
        return

    if run.dry_run:
        run.planned += len(missing)
        print(f"  +  {device_type.model}: {len(missing)} {kind}  (would create)")
        return

    payloads = []
    for m in missing:
        parent = ports.get(m["_parent_port"])
        if parent is None:
            raise RuntimeError(
                f"{device_type.model}: outlet {m['name']} names power port "
                f"'{m['_parent_port']}', which does not exist on this type"
            )
        payloads.append({
            "name": m["name"],
            "type": m["type"],
            "device_type": device_type.id,
            "power_port": parent,
        })

    run.nb.dcim.power_outlet_templates.create(payloads)
    run.created += len(missing)
    run.existing += len(wanted) - len(missing)
    print(f"  +  {device_type.model}: {len(missing)} {kind} created")


def build_devices(run, spec):
    """Device roles, then devices declared as groups with computed positions."""

    print("\ndevice roles")
    roles = {}
    for role_spec in spec.get("device_roles", []):
        roles[role_spec["slug"]] = run.ensure(
            run.nb.dcim.device_roles,
            {"slug": role_spec["slug"]},
            {
                "name": role_spec["name"],
                "slug": role_spec["slug"],
                "color": role_spec["color"],
            },
            role_spec["name"],
        )

    site = run.nb.dcim.sites.get(slug=spec["site"]["slug"])

    for group in spec.get("device_groups", []):
        print(f"\n{group['description']}")

        device_type = run.nb.dcim.device_types.get(slug=group["device_type"])
        role = roles.get(group["role"])

        entries = []
        index = 0
        for rack_name in group["racks"]:
            rack = run.nb.dcim.racks.get(name=rack_name, site_id=site.id)

            for n in range(1, group["per_rack"] + 1):
                index += 1
                name = group["name_template"].format(
                    rack=rack_name,
                    rack_lower=rack_name.lower(),
                    n=n,
                    g=index,
                    letter=chr(ord("a") + n - 1),
                )

                payload = {
                    "name": name,
                    "device_type": device_type.id if device_type else None,
                    "role": role.id if role else None,
                    "site": site.id,
                    "rack": rack.id if rack else None,
                    "status": "active",
                }

                # An explicit list wins over start/step, because real layouts
                # are not always a uniform stride: NVIDIA's compute rack puts
                # a 3U airflow gap between the second and third DGX, which no
                # single step value can express.
                # 0U devices omit both, and their position and face stay unset.
                if "positions" in group:
                    payload["position"] = group["positions"][n - 1]
                    payload["face"] = group.get("face", "front")
                elif "start_position" in group:
                    step = group.get("position_step", 1)
                    payload["position"] = group["start_position"] + (n - 1) * step
                    payload["face"] = group.get("face", "front")

                entries.append((name, rack, payload))

        # Pre-pass: vacate any rack unit that is about to change.
        #
        # NetBox rejects a device whose position overlaps another, and a stack
        # shifting upward collides with its own neighbour -- moving a DGX from
        # U1 to U3 overlaps the system still sitting at U9. Moving top-down
        # would work for an upward shift and fail for a downward one, so
        # instead every mover is set to no position first, then placed. Two
        # calls per device, and correct regardless of which way things move.
        if not run.dry_run:
            movers = []
            for name, _, payload in entries:
                target = payload.get("position")
                if target is None:
                    continue
                current = run.nb.dcim.devices.get(name=name)
                if current is None or current.position is None:
                    continue
                if float(current.position) != float(target):
                    movers.append(current)
            if movers:
                print(f"  .  vacating {len(movers)} positions before re-placing")
                for device in movers:
                    device.update({"position": None})

        for name, rack, payload in entries:
            run.ensure(
                run.nb.dcim.devices,
                {"name": name},
                payload,
                name,
                depends_on=(device_type, role, rack),
            )


def field_value(obj, field):
    """Read a field for comparison, unwrapping pynetbox choice records.

    Choice fields (type, airflow...) come back as a Record carrying .value;
    plain fields come back as themselves. Comparing the Record directly
    against a spec string would report drift on every single run.
    """
    value = getattr(obj, field, None)
    return getattr(value, "value", value)


def sync_device_power_ports(run):
    """Push device-type power-port draw values down onto existing devices.

    NetBox stamps components onto a device from its type at CREATION time and
    never revisits them. Fixing a template therefore leaves every already-built
    device wrong -- the asymmetry that bit this project three times. This makes
    the type authoritative after the fact.
    """
    print("\nsync device power ports to their device types")

    templates = {}
    for t in run.nb.dcim.power_port_templates.all():
        if t.device_type:
            templates[(t.device_type.id, t.name)] = t

    # A power port's .device is a BRIEF nested record -- id, name, url and
    # nothing else. It carries no device_type, so the mapping has to come from
    # the devices endpoint. Fetched once rather than per port.
    device_type_of = {d.id: d.device_type.id for d in run.nb.dcim.devices.all()}

    changed = 0
    for port in run.nb.dcim.power_ports.all():
        device_type_id = device_type_of.get(port.device.id)
        if device_type_id is None:
            continue
        template = templates.get((device_type_id, port.name))
        if template is None:
            continue
        diff = {}
        for field in ("maximum_draw", "allocated_draw"):
            wanted = field_value(template, field)
            if field_value(port, field) != wanted:
                diff[field] = wanted
        if not diff:
            continue
        summary = ", ".join(f"{k}={v!r}" for k, v in diff.items())
        if run.dry_run:
            run.planned_updates += 1
            print(f"  ~  {port.device.name}/{port.name}: {summary}  (would update)")
        else:
            port.update(diff)
            run.updated += 1
            changed += 1

    if not changed and not run.dry_run:
        print("  =  all device power ports match their templates")
    elif changed:
        print(f"  ~  {changed} device power ports updated")


def natural_key(name):
    """Sort outlet1 < outlet2 < outlet10, not outlet1 < outlet10 < outlet2."""
    match = re.search(r"(\d+)$", name)
    if match:
        return (name[: match.start()], int(match.group(1)))
    return (name, 0)


def assign_sources(num_ports, device_index):
    """Which of the three rack rPDUs each of a device's PSUs plugs into.

    Six supplies spread a,a,b,b,c,c -- two per source, so losing any one
    source costs exactly two supplies, leaving the four a DGX H100 needs.

    Two supplies cannot reach all three sources, so the starting point
    rotates per device. Without that rotation every 2-PSU switch would land
    on a and b, and source c would carry nothing.
    """
    letters = ["a", "b", "c"]
    if num_ports >= 3:
        return [letters[(j * 3) // num_ports] for j in range(num_ports)]
    start = device_index % 3
    return [letters[(start + j) % 3] for j in range(num_ports)]


def build_power(run, spec):
    """Power panels, one feed per panel per rack, and all power cabling."""

    site = run.nb.dcim.sites.get(slug=spec["site"]["slug"])
    location = run.nb.dcim.locations.get(slug=spec["location"]["slug"])

    print("\npower panels")
    panels = {}
    for panel_name in spec.get("power_panels", []):
        panels[panel_name] = run.ensure(
            run.nb.dcim.power_panels,
            {"name": panel_name, "site_id": site.id},
            {
                "name": panel_name,
                "site": site.id,
                "location": location.id if location else None,
            },
            panel_name,
        )

    feed_spec = spec.get("power_feeds")
    if not feed_spec:
        return

    print("\npower feeds")
    feeds_missing = False
    racks = [r["name"] for r in spec["racks"]]
    for rack_name in racks:
        rack = run.nb.dcim.racks.get(name=rack_name, site_id=site.id)
        for panel_name, letter in zip(feed_spec["panels"], feed_spec["letters"]):
            panel = panels.get(panel_name)
            name = feed_spec["name_template"].format(rack=rack_name, letter=letter)
            feed = run.ensure(
                run.nb.dcim.power_feeds,
                {"name": name, "rack_id": rack.id} if rack else {"name": name},
                {
                    "name": name,
                    "power_panel": panel.id if panel else None,
                    "rack": rack.id if rack else None,
                    "status": feed_spec["status"],
                    "type": feed_spec["type"],
                    "supply": feed_spec["supply"],
                    "phase": feed_spec["phase"],
                    "voltage": feed_spec["voltage"],
                    "amperage": feed_spec["amperage"],
                    "max_utilization": feed_spec["max_utilization"],
                },
                name,
                depends_on=(rack, panel),
            )
            if feed is None or isinstance(feed, Planned):
                feeds_missing = True

    if run.dry_run and feeds_missing:
        # Cabling terminates on real feed, outlet and port ids. If any feed is
        # still only planned, previewing a cable count would mean inventing
        # relationships between objects that have no ids -- say so instead.
        # Note this tests for MISSING FEEDS specifically, not for any pending
        # change: a pending attribute update elsewhere does not stop us
        # previewing cabling accurately.
        print("\npower cabling")
        print("  .  skipped in dry run until feeds exist (re-run --dry-run after apply)")
        return

    build_power_cabling(run, spec, site)


def build_power_cabling(run, spec, site):
    """Cable PDU inputs to feeds, then every device PSU to a PDU outlet.

    Idempotent by checking whether each termination already carries a cable,
    and by always taking the lowest-numbered free outlet -- so re-runs are
    stable and the hand-cabled C01 is left exactly as it is.
    """
    print("\npower cabling")

    # Fetch once and group client-side rather than filtering per device.
    all_devices = list(run.nb.dcim.devices.all())
    all_ports = list(run.nb.dcim.power_ports.all())
    all_outlets = list(run.nb.dcim.power_outlets.all())
    all_feeds = list(run.nb.dcim.power_feeds.all())

    feeds_by_name = {f.name: f for f in all_feeds}
    ports_by_device = {}
    for p in all_ports:
        ports_by_device.setdefault(p.device.id, []).append(p)
    outlets_by_device = {}
    for o in all_outlets:
        outlets_by_device.setdefault(o.device.id, []).append(o)

    pending = []
    used_outlets = {o.id for o in all_outlets if o.cable}

    for rack_spec in spec["racks"]:
        rack_name = rack_spec["name"]
        rack = run.nb.dcim.racks.get(name=rack_name, site_id=site.id)
        in_rack = [d for d in all_devices if d.rack and d.rack.id == rack.id]

        pdus = {}
        for d in in_rack:
            if d.name.startswith(f"pdu-{rack_name.lower()}-"):
                pdus[d.name[-1]] = d
        if len(pdus) != 3:
            print(f"  .  {rack_name}: expected 3 rPDUs, found {len(pdus)} -- skipped")
            run.skipped += 1
            continue

        # rPDU input -> power feed
        for letter, pdu in sorted(pdus.items()):
            inputs = [p for p in ports_by_device.get(pdu.id, []) if p.name == "input"]
            if not inputs or inputs[0].cable:
                continue
            feed = feeds_by_name.get(f"{rack_name}-{letter.upper()}")
            if feed is None or feed.cable:
                continue
            pending.append({
                "a_terminations": [{"object_type": "dcim.powerfeed", "object_id": feed.id}],
                "b_terminations": [{"object_type": "dcim.powerport", "object_id": inputs[0].id}],
                "status": "connected",
                "type": "power",
            })

        # device PSU -> rPDU outlet
        powered = sorted(
            [d for d in in_rack if not d.name.startswith(f"pdu-{rack_name.lower()}-")],
            key=lambda d: natural_key(d.name),
        )
        for device_index, device in enumerate(powered):
            ports = sorted(
                ports_by_device.get(device.id, []), key=lambda p: natural_key(p.name)
            )
            if not ports:
                continue
            for port, letter in zip(ports, assign_sources(len(ports), device_index)):
                if port.cable:
                    continue
                pdu = pdus[letter]
                free = [
                    o for o in sorted(
                        outlets_by_device.get(pdu.id, []), key=lambda o: natural_key(o.name)
                    )
                    if o.id not in used_outlets
                ]
                if not free:
                    raise RuntimeError(
                        f"{pdu.name} has no free outlet for {device.name}/{port.name}"
                    )
                outlet = free[0]
                used_outlets.add(outlet.id)
                pending.append({
                    "a_terminations": [{"object_type": "dcim.poweroutlet", "object_id": outlet.id}],
                    "b_terminations": [{"object_type": "dcim.powerport", "object_id": port.id}],
                    "status": "connected",
                    "type": "power",
                })

    if not pending:
        print("  =  all power cabling present")
        return

    if run.dry_run:
        run.planned += len(pending)
        print(f"  +  {len(pending)} power cables  (would create)")
        return

    # Batched so one oversized payload cannot fail the whole set.
    for start in range(0, len(pending), 100):
        batch = pending[start:start + 100]
        run.nb.dcim.cables.create(batch)
        run.created += len(batch)
        print(f"  +  {len(batch)} power cables created")


def build_fabric_cabling(run, spec):
    """Rail-optimised compute fabric, leaf-spine uplinks, storage, in-band, OOB.

    Every mapping here is explicit arithmetic rather than "next free port",
    because in a rail-optimised fabric WHICH port a cable lands on is the
    design. Rail 3 on node 1 and rail 3 on node 32 must reach the same leaf;
    a nearest-free-port allocator would produce a working-looking fabric with
    none of the locality the topology exists to provide.
    """
    cfg = spec.get("fabric_cabling")
    if not cfg:
        return

    print("\nfabric cabling")

    devices = {d.name: d for d in run.nb.dcim.devices.all()}
    by_device = {}
    for iface in run.nb.dcim.interfaces.all():
        by_device.setdefault(iface.device.id, {})[iface.name] = iface

    def port(device_name, iface_name):
        device = devices.get(device_name)
        if device is None:
            return None
        return by_device.get(device.id, {}).get(iface_name)

    pending = []
    missing = []

    def link(a, b, settings, label):
        if a is None or b is None:
            missing.append(label)
            return
        if a.cable or b.cable:
            return
        pending.append({
            "a_terminations": [{"object_type": "dcim.interface", "object_id": a.id}],
            "b_terminations": [{"object_type": "dcim.interface", "object_id": b.id}],
            "status": "connected",
            "type": settings["cable_type"],
            "length": settings["length_m"],
            "length_unit": "m",
        })

    nodes = sorted(
        [n for n in devices if n.startswith("dgx-")], key=natural_key
    )
    storage_nodes = sorted(
        [n for n in devices if n.startswith("storage-")], key=natural_key
    )

    # 1. Compute rails: node i, rail r -> leaf-(r+1), port (i+1).
    rails = cfg["compute_rails"]
    for i, node in enumerate(nodes):
        for r in range(8):
            link(
                port(node, f"ib-rail{r}"),
                port(f"leaf-{r + 1:02d}", f"port{i + 1}"),
                rails,
                f"{node}/ib-rail{r}",
            )

    # 2. Leaf-spine uplinks, 8 per pair. Leaf uplink ports start at 33;
    #    spine ports fill 1-64 exactly across 8 leaves x 8 links.
    up = cfg["uplinks"]
    per_pair = up["per_leaf_spine_pair"]
    for leaf_index in range(1, 9):
        for spine_index in range(1, 5):
            for k in range(per_pair):
                leaf_port = up["leaf_uplink_start_port"] + (spine_index - 1) * per_pair + k
                spine_port = (leaf_index - 1) * per_pair + k + 1
                link(
                    port(f"leaf-{leaf_index:02d}", f"port{leaf_port}"),
                    port(f"spine-{spine_index:02d}", f"port{spine_port}"),
                    up,
                    f"leaf-{leaf_index:02d}/port{leaf_port}",
                )

    # 3. Storage fabric: two per node, one to each storage-fabric switch.
    #    Compute nodes take ports 1-32; storage nodes follow at 33+.
    stor = cfg["storage"]
    for i, node in enumerate(nodes):
        for j in range(2):
            link(
                port(node, f"ib-storage{j}"),
                port(f"stor-fabric-{j + 1:02d}", f"port{i + 1}"),
                stor,
                f"{node}/ib-storage{j}",
            )
    for m, node in enumerate(storage_nodes):
        for j in range(2):
            link(
                port(node, f"ib-storage{j}"),
                port(f"stor-fabric-{j + 1:02d}", f"port{len(nodes) + m + 1}"),
                stor,
                f"{node}/ib-storage{j}",
            )

    # 4. In-band management: two per node across the two SN4600C.
    inband = cfg["in_band"]
    for i, node in enumerate(nodes):
        for j in range(2):
            link(
                port(node, f"inband{j}"),
                port(f"inband-{j + 1:02d}", f"eth{i + 1}"),
                inband,
                f"{node}/inband{j}",
            )
    for m, node in enumerate(storage_nodes):
        for j in range(2):
            link(
                port(node, f"eth{j}"),
                port(f"inband-{j + 1:02d}", f"eth{len(nodes) + m + 1}"),
                inband,
                f"{node}/eth{j}",
            )

    # 5. Out-of-band BMC: 36 endpoints split evenly across two SN2201.
    oob = cfg["out_of_band"]
    bmc_devices = nodes + storage_nodes
    half = (len(bmc_devices) + 1) // 2
    for i, node in enumerate(bmc_devices):
        switch = 1 if i < half else 2
        switch_port = (i % half) + 1
        link(
            port(node, "bmc"),
            port(f"oob-{switch:02d}", f"eth{switch_port}"),
            oob,
            f"{node}/bmc",
        )

    # 6. UFM appliances: management ethernet only.
    #
    # Their InfiniBand ports are deliberately NOT cabled. A strictly 1:1
    # non-blocking fabric consumes every port -- 32 down + 32 up on each leaf,
    # and 8 leaves x 8 links fills all 64 ports on each spine -- so there is no
    # free port to attach them to. Inventing one would quietly break the
    # non-blocking property the topology exists to provide. Real deployments
    # reserve fabric ports for management at the cost of slight
    # oversubscription; leaving these uncabled makes that trade-off visible.
    #
    # Explicit port 20 on each OOB switch, rather than extending the allocator
    # above: that splits 36 endpoints evenly across two switches, and adding
    # two more would shift every index and collide with cables already placed.
    ufms = sorted([n for n in devices if n.startswith("ufm-")], key=natural_key)
    for i, node in enumerate(ufms):
        link(
            port(node, "eth0"),
            port(f"oob-{i + 1:02d}", "eth20"),
            oob,
            f"{node}/eth0",
        )

    if missing:
        run.skipped += len(missing)
        print(f"  .  {len(missing)} endpoints not found, e.g. {missing[0]}")

    if not pending:
        print("  =  all fabric cabling present")
        return

    if run.dry_run:
        run.planned += len(pending)
        print(f"  +  {len(pending)} fabric cables  (would create)")
        return

    for start in range(0, len(pending), 100):
        batch = pending[start:start + 100]
        run.nb.dcim.cables.create(batch)
        run.created += len(batch)
        print(f"  +  {len(batch)} fabric cables created")


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
                entry = {"name": name}
                # type and the draw fields are all optional. Omitting the draw
                # fields is load-bearing for pass-through ports: NetBox only
                # aggregates downstream load when BOTH are empty.
                for field in ("type", "maximum_draw", "allocated_draw", "description"):
                    if field in pp:
                        entry[field] = pp[field]
                power_ports.append(entry)
        if power_ports:
            ensure_components(
                run, run.nb.dcim.power_port_templates,
                device_type, power_ports, "power ports",
            )

        # Outlets carry a foreign key to their parent power port template, so
        # they must be created after it and need its id -- not just its name.
        outlets = []
        for po in dt_spec.get("power_outlets", []):
            for name in expand(po["name"]):
                outlets.append({
                    "name": name,
                    "type": po["type"],
                    "_parent_port": po["power_port"],
                })
        if outlets:
            ensure_outlets(run, device_type, outlets)

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
        build_devices(run, spec)
        sync_device_power_ports(run)
        build_power(run, spec)
        build_fabric_cabling(run, spec)
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
