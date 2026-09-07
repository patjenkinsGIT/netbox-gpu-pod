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
    python scripts/build_pod.py --spec spec/pod-gb200.yaml

Two specs exist. spec/pod.yaml is the air-cooled DGX H100 scalable unit and is
the default; spec/pod-gb200.yaml is the liquid-cooled GB200 NVL72 unit in a
separate site. Objects declared in both -- the region, shared device types --
are reconciled to the same values by either, which is a check on both files.
"""

import argparse
import os
import re
import sys

import pynetbox
import requests
import yaml

from netbox_client import REPO_ROOT, ConfigError, connect


def differs(value, declared):
    """Whether a stored value disagrees with a declared one.

    NetBox returns numerics as strings or Decimals depending on the field, so
    a declared 12 comes back as "12.00" and a naive != reports drift on every
    run forever. A reconciler that always thinks something changed is as
    useless as one that never does -- the same failure the pynetbox choice
    Record caused in S4, arriving this time through decimals on cooling
    diameters and flow rates.
    """
    if value is None or declared is None:
        return value != declared
    try:
        return float(value) != float(declared)
    except (TypeError, ValueError):
        return value != declared


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
        if differs(value, declared):
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
            if not field.startswith("_") and field != "name"
            and differs(field_value(current, field), value)
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


def ensure_parented(run, device_type, wanted, kind,
                    parent_endpoint, child_endpoint, parent_field):
    """Create downstream templates, resolving each one's parent by name.

    Power outlets reference their power port by id; cooling outflows reference
    their cooling intake by id. Without that link NetBox has no path from a
    connected device up to the source, and the numbers silently read zero --
    the failure that cost an hour in S3, and which the 4.7 cooling model
    reproduces exactly, down to the shape of the foreign key.

    Generalised rather than copied because the two are structurally identical:
    a set of children on a device type, each naming one parent on the same
    type. Two copies would be two places to fix the next time the parenting
    rule bites.
    """
    if isinstance(device_type, Planned):
        run.planned += len(wanted)
        print(f"  +  {device_type.label}: {len(wanted)} {kind}  (would create)")
        return

    parents = {
        p.name: p.id for p in parent_endpoint.all()
        if p.device_type and p.device_type.id == device_type.id
    }
    existing = {
        o.name for o in child_endpoint.all()
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
        parent = parents.get(m["_parent"])
        if parent is None:
            raise RuntimeError(
                f"{device_type.model}: {kind[:-1]} {m['name']} names parent "
                f"'{m['_parent']}', which does not exist on this type"
            )
        payload = {
            k: v for k, v in m.items() if not k.startswith("_")
        }
        payload["device_type"] = device_type.id
        payload[parent_field] = parent
        payloads.append(payload)

    child_endpoint.create(payloads)
    run.created += len(missing)
    run.existing += len(wanted) - len(missing)
    print(f"  +  {device_type.model}: {len(missing)} {kind} created")


def site_or_preview(run, spec):
    """Resolve the site, or return None so the caller can preview instead.

    build_devices, build_power and build_cooling each look the site up fresh
    and then dereference it. That was safe for the H100 pod only because S1
    created the site by hand, so every later dry run found one waiting.
    Previewing a spec whose site does not exist yet -- which is exactly what
    "reproduces the model from scratch" is supposed to mean -- crashed on the
    first attribute access.

    The bug is older than the GB200 spec and applies to pod.yaml too: a dry
    run against a genuinely empty NetBox has never worked. It survived because
    the generator was only ever previewed against an instance that already had
    the site, which is the same shape of mistake as testing a reconciler only
    on objects it had itself just created.
    """
    site = run.nb.dcim.sites.get(slug=spec["site"]["slug"])
    if site is not None:
        return site
    if not run.dry_run:
        # Not reachable through main(), since build_racks creates the site
        # first. Raised rather than returning None so that if it ever does
        # happen it says so instead of quietly building nothing.
        raise RuntimeError(
            f"site {spec['site']['slug']!r} does not exist and this is not a "
            f"dry run -- build_racks should have created it"
        )
    return None


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

    site = site_or_preview(run, spec)

    for group in spec.get("device_groups", []):
        print(f"\n{group['description']}")

        # Counted from the spec rather than skipped, because a preview that
        # understates what an apply does is a preview people stop reading --
        # the reason the Planned sentinel exists at all. The arithmetic here
        # is the same arithmetic the loop below performs, so the number is
        # the real one even though no object can be looked up yet.
        if site is None:
            count = len(group["racks"]) * group["per_rack"]
            run.planned += count
            print(f"  +  {count} x {group['device_type']}  "
                  f"(would create, after {spec['site']['name']})")
            continue

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

    # Counted separately from run.updated so the section can say something in
    # every case. Printing nothing when there is no drift reads as "this step
    # did not run", which is the wrong thing for a silent-failure-prone pass
    # to say -- and in a dry run it printed nothing at all.
    found = 0
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
        found += 1
        summary = ", ".join(f"{k}={v!r}" for k, v in diff.items())
        if run.dry_run:
            run.planned_updates += 1
            print(f"  ~  {port.device.name}/{port.name}: {summary}  (would update)")
        else:
            port.update(diff)
            run.updated += 1
            changed += 1

    if not found:
        print("  =  all device power ports match their templates")
    elif run.dry_run:
        print(f"  ~  {found} device power ports would be updated")
    else:
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

    site = site_or_preview(run, spec)
    if site is None:
        feed_spec = spec.get("power_feeds") or {}
        per_rack = len(feed_spec.get("letters") or []) or feed_spec.get("count", 0)
        fed = feed_spec.get("racks") or [r["name"] for r in spec["racks"]]

        print("\npower panels")
        run.planned += len(spec.get("power_panels", []))
        print(f"  +  {len(spec.get('power_panels', []))} panels  "
              f"(would create, after {spec['site']['name']})")
        print("\npower feeds")
        run.planned += len(fed) * per_rack
        print(f"  +  {len(fed) * per_rack} feeds  "
              f"(would create, after {spec['site']['name']})")
        print("\npower cabling")
        print("  .  previews once devices exist (re-run --dry-run after apply)")
        return

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

    # Not every rack in a pod necessarily takes feeds. The H100 pod fed all
    # twelve because all twelve held IEC-powered equipment; the GB200 pod's
    # CDU rack has no distribution modelled in it yet, and giving it whips to
    # nothing would look tidier than the truth.
    racks = feed_spec.get("racks") or [r["name"] for r in spec["racks"]]

    # Two shapes, because two redundancy schemes.
    #
    # `letters` zips a panel list against a letter list: three panels, three
    # feeds, A/B/C -- N+1 at 50% sizing, which is what NVIDIA's H100
    # electrical guidance specifies.
    #
    # `count` puts N feeds per rack and rotates through the panels: eight
    # power shelves alternating across two panels -- N+N, which is what
    # NVIDIA states for the NVL72. Alternation matters. Filling panel D
    # before starting panel E would put all eight shelves' worth of
    # first-choice load on one side and defeat the split entirely.
    panel_names = feed_spec["panels"]
    if "letters" in feed_spec:
        slots = [
            (panel, {"n": i + 1, "letter": letter})
            for i, (panel, letter) in enumerate(
                zip(panel_names, feed_spec["letters"])
            )
        ]
    else:
        slots = [
            (panel_names[i % len(panel_names)],
             {"n": i + 1, "letter": chr(ord("A") + i)})
            for i in range(feed_spec["count"])
        ]

    for rack_name in racks:
        rack = run.nb.dcim.racks.get(name=rack_name, site_id=site.id)
        for panel_name, fields in slots:
            panel = panels.get(panel_name)
            name = feed_spec["name_template"].format(rack=rack_name, **fields)
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

    # Which chain to build. "rpdu" is the H100 pod's feed -> rPDU -> outlet ->
    # PSU; "busbar" is the NVL72's feed -> shelf -> bus -> tray. Declared in
    # the spec rather than inferred from device names, because inferring it
    # from a "pdu-" prefix is precisely the assumption that made this
    # generator only work on one pod.
    topology = (spec.get("power_cabling") or {}).get("topology", "rpdu")
    if topology == "busbar":
        build_busbar_cabling(run, spec, site)
    else:
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


def build_busbar_cabling(run, spec, site):
    """Cable the NVL72 chain: feed -> power shelf -> DC busbar -> tray.

    Four hops where the rPDU chain has three, because AC/DC conversion is a
    rack component here rather than something inside each server's supply.

    THE FICTION, STATED ONCE IN CODE AS WELL AS IN THE SPEC. A busbar is a
    single shared plane: eight shelves push into it and twenty-seven trays
    pull from it, with no correspondence between any particular shelf and any
    particular tray. NetBox's power model is a tree of one-to-one cables and
    cannot hold that. So trays are dealt round-robin across the shelves, which
    makes the totals aggregate correctly to the feeds and asserts something
    false about the topology.

    What that costs: the H100 pod's N-1 verdict was DERIVED, by walking cables
    NetBox already held. Round-robin models normal operation only, so the
    NVL72's N+N property cannot be walked out of this model the same way. The
    power report has to assert it, and say that it is asserting it.
    """
    cfg = spec.get("power_cabling") or {}
    print("\npower cabling (busbar)")

    devices = list(run.nb.dcim.devices.all())
    ports = {}
    for p in run.nb.dcim.power_ports.all():
        ports.setdefault(p.device.id, {})[p.name] = p
    outlets = {}
    for o in run.nb.dcim.power_outlets.all():
        outlets.setdefault(o.device.id, []).append(o)
    feeds_by_rack = {}
    for f in run.nb.dcim.power_feeds.all():
        if f.rack:
            feeds_by_rack.setdefault(f.rack.id, []).append(f)

    shelf_type = cfg.get("shelf_type")
    busbar_type = cfg.get("busbar_type")
    pending = []

    for rack_spec in spec["racks"]:
        rack_name = rack_spec["name"]
        rack = run.nb.dcim.racks.get(name=rack_name, site_id=site.id)
        if rack is None:
            continue
        in_rack = [d for d in devices if d.rack and d.rack.id == rack.id]

        shelves = sorted(
            [d for d in in_rack if d.device_type.slug == shelf_type],
            key=lambda d: natural_key(d.name),
        )
        busbars = [d for d in in_rack if d.device_type.slug == busbar_type]
        if not shelves or not busbars:
            continue
        busbar = busbars[0]

        # A tray is anything in the rack that takes a DC feed off the bus.
        # Identified by the port its own device type declares, not by a name
        # prefix -- the ToR switches sit in this rack too, with IEC inlets and
        # no bus connection, and they must not be swept in.
        trays = sorted(
            [
                d for d in in_rack
                if d.id not in {busbar.id} | {s.id for s in shelves}
                and "dc-in" in ports.get(d.id, {})
            ],
            key=lambda d: natural_key(d.name),
        )

        expected_shelves = cfg.get("shelves_per_rack")
        expected_trays = cfg.get("trays_per_rack")
        if expected_shelves and len(shelves) != expected_shelves:
            print(f"  .  {rack_name}: expected {expected_shelves} shelves, "
                  f"found {len(shelves)} -- skipped")
            run.skipped += 1
            continue
        if expected_trays and len(trays) != expected_trays:
            print(f"  .  {rack_name}: expected {expected_trays} trays, "
                  f"found {len(trays)} -- skipped")
            run.skipped += 1
            continue

        # 1. Feed -> shelf input. Feeds are taken in name order against
        #    shelves in name order, so shelf N sits on the feed the spec's
        #    panel alternation put at position N.
        rack_feeds = sorted(
            feeds_by_rack.get(rack.id, []), key=lambda f: natural_key(f.name)
        )
        for feed, shelf in zip(rack_feeds, shelves):
            port = ports.get(shelf.id, {}).get("input")
            if port is None or port.cable or feed.cable:
                continue
            pending.append({
                "a_terminations": [{"object_type": "dcim.powerfeed", "object_id": feed.id}],
                "b_terminations": [{"object_type": "dcim.powerport", "object_id": port.id}],
                "status": "connected",
                "type": "power",
            })

        # 2. Shelf DC output -> the matching busbar input.
        for i, shelf in enumerate(shelves, start=1):
            out = next(
                (o for o in outlets.get(shelf.id, []) if o.name == "dc-out"),
                None,
            )
            bus_port = ports.get(busbar.id, {}).get(f"shelf{i}")
            if out is None or bus_port is None or out.cable or bus_port.cable:
                continue
            pending.append({
                "a_terminations": [{"object_type": "dcim.poweroutlet", "object_id": out.id}],
                "b_terminations": [{"object_type": "dcim.powerport", "object_id": bus_port.id}],
                "status": "connected",
                "type": "power",
            })

        # 3. Busbar -> trays, round-robin across the shelves.
        #
        # Grouped by each outlet's declared parent port, not by parsing outlet
        # names. The parent is the thing that actually determines which shelf
        # a tray's load lands on; a name is just a label that happens to
        # agree today.
        by_parent = {}
        for o in outlets.get(busbar.id, []):
            if o.power_port:
                by_parent.setdefault(o.power_port.name, []).append(o)
        for group in by_parent.values():
            group.sort(key=lambda o: natural_key(o.name))

        # Assignment is by LOAD, not by position.
        #
        # Dealing trays round-robin looks balanced and is not: the tray list
        # sorts by name, so eighteen 5400 W compute trays take the first
        # eighteen slots and nine 2500 W NVLink trays trail behind. Shelves 1
        # and 2 collected three heavy trays each and read 18,700 VA while
        # shelves 4-8 read 13,300 -- a 40% spread invented entirely by
        # alphabetical order.
        #
        # That matters because it does not stay inside the model. On a real
        # shared bus every shelf carries the same 119,700 / 8 = 14,962 VA. The
        # round-robin version reported a worst feed of 54.2% against a true
        # 43.4%, and that number would have gone into the power report as a
        # hotspot with no physical existence.
        #
        # Heaviest-first onto the least-loaded shelf (longest-processing-time
        # scheduling) gets as close to the real behaviour as a tree can. It
        # cannot get all the way: 119,700 does not divide evenly into eight
        # discrete trays, so a spread of roughly 13,300-16,200 remains. THE
        # PER-FEED NUMBER IS THEREFORE STILL A MODELLING ARTEFACT. The rack
        # total is the figure that is real; the exports must say which is
        # which rather than presenting both as measurements.
        def tray_draw(device):
            port = ports.get(device.id, {}).get("dc-in")
            return float(getattr(port, "allocated_draw", None) or 0)

        capacity = {
            i: len(by_parent.get(f"shelf{i}", []))
            for i in range(1, len(shelves) + 1)
        }
        load = {i: 0.0 for i in capacity}
        taken = {i: 0 for i in capacity}
        assignment = {}

        # Planned over ALL trays, not just uncabled ones, so an interrupted
        # run resumes onto the same plan instead of a different one.
        for tray in sorted(trays, key=lambda d: (-tray_draw(d), natural_key(d.name))):
            options = [i for i in capacity if taken[i] < capacity[i]]
            if not options:
                raise RuntimeError(
                    f"{busbar.name}: no free outlet anywhere for {tray.name}"
                )
            best = min(options, key=lambda i: (load[i], i))
            assignment[tray.id] = best
            load[best] += tray_draw(tray)
            taken[best] += 1

        spread = f"{min(load.values()):.0f}-{max(load.values()):.0f} VA"
        print(f"  .  {rack_name}: shelf load spread {spread} "
              f"(shared bus would be {sum(load.values()) / len(load):.0f})")

        used = {o.id for group in by_parent.values() for o in group if o.cable}
        for tray in trays:
            port = ports.get(tray.id, {}).get("dc-in")
            if port is None or port.cable:
                continue
            parent = f"shelf{assignment[tray.id]}"
            free = [o for o in by_parent.get(parent, []) if o.id not in used]
            if not free:
                raise RuntimeError(
                    f"{busbar.name}: no free outlet on {parent} for "
                    f"{tray.name}/dc-in"
                )
            outlet = free[0]
            used.add(outlet.id)
            pending.append({
                "a_terminations": [{"object_type": "dcim.poweroutlet", "object_id": outlet.id}],
                "b_terminations": [{"object_type": "dcim.powerport", "object_id": port.id}],
                "status": "connected",
                "type": "power",
            })

    if not pending:
        print("  =  all busbar power cabling present")
        return

    if run.dry_run:
        run.planned += len(pending)
        print(f"  +  {len(pending)} power cables  (would create)")
        return

    for start in range(0, len(pending), 100):
        batch = pending[start:start + 100]
        run.nb.dcim.cables.create(batch)
        run.created += len(batch)
        print(f"  +  {len(batch)} power cables created")


def build_cooling(run, spec):
    """Cooling plant, per-rack feeds, and the intake -> outflow chain.

    NOTHING HERE AGGREGATES, and that is the finding rather than a defect in
    this function. NetBox 4.7's cooling model carries capacity on the source,
    the feed and the rack, and demand nowhere -- a cooling intake has no
    allocated_draw equivalent. It also has no cable, and no foreign key from a
    CoolingFeed to any device component.

    So this builds a topology NetBox will hold and will not compute from. Every
    cooling utilisation figure in the exports is derived by us from the power
    model, and has to be labelled derived. In S6 the N-1 verdict counted as
    independent reproduction because NetBox walked the path and we only read
    the answer off. That is not available here.
    """
    cfg = spec.get("cooling")
    if not cfg:
        return

    site = site_or_preview(run, spec)
    if site is None:
        print("\ncooling sources")
        run.planned += len(cfg.get("sources", []))
        print(f"  +  {len(cfg.get('sources', []))} sources  "
              f"(would create, after {spec['site']['name']})")
        print("\ncooling feeds")
        run.planned += len(cfg.get("feeds", []))
        print(f"  +  {len(cfg.get('feeds', []))} feeds  "
              f"(would create, after {spec['site']['name']})")
        print("\ncooling topology")
        print("  .  previews once devices exist (re-run --dry-run after apply)")
        return

    location = run.nb.dcim.locations.get(slug=spec["location"]["slug"])

    print("\ncooling sources")
    sources = {}
    for s in cfg.get("sources", []):
        payload = {
            "name": s["name"],
            "site": site.id,
            "type": s["type"],
            "fluid_type": s["fluid_type"],
            "status": s.get("status", "active"),
            "description": s.get("description", ""),
        }
        if "cooling_capacity" in s:
            payload["cooling_capacity"] = s["cooling_capacity"]
        if location:
            payload["location"] = location.id
        sources[s["name"]] = run.ensure(
            run.nb.dcim.cooling_sources,
            {"name": s["name"], "site_id": site.id},
            payload,
            s["name"],
            depends_on=(site,),
        )

    print("\ncooling feeds")
    for f in cfg.get("feeds", []):
        source = sources.get(f["source"])
        rack = run.nb.dcim.racks.get(name=f["rack"], site_id=site.id)
        payload = {
            "name": f["name"],
            "cooling_source": source.id if source else None,
            "rack": rack.id if rack else None,
            "status": f.get("status", "active"),
            "description": f.get("description", ""),
        }
        for field in ("cooling_capacity", "max_flow", "max_flow_unit"):
            if field in f:
                payload[field] = f[field]
        run.ensure(
            run.nb.dcim.cooling_feeds,
            {"name": f["name"]},
            payload,
            f["name"],
            depends_on=(source, rack),
        )

    topology = cfg.get("topology")
    if not topology:
        return

    print("\ncooling topology")

    devices = {d.name: d for d in run.nb.dcim.devices.all()}
    intakes = {}
    for i in run.nb.dcim.cooling_intakes.all():
        intakes.setdefault(i.device.id, {})[i.name] = i
    outflows = {}
    for o in run.nb.dcim.cooling_outflows.all():
        outflows.setdefault(o.device.id, {})[o.name] = o

    racks_per_cdu = topology["racks_per_cdu"]

    # A rack is in the cooling topology if it has a manifold in it. Taken in
    # spec order so CDU assignment is stable: racks 1-4 to cdu-01, 5-8 to
    # cdu-02. Deriving it from the model rather than from a second list in the
    # spec means the two cannot drift apart.
    cooled_racks = [
        r["name"] for r in spec["racks"]
        if f"manifold-{r['name'].lower()}" in devices
    ]

    # Intakes are stamped onto a device from its type when the device is
    # created, so before an apply there is nothing to wire. Say that, rather
    # than looping over nothing and reporting "all wired" -- a pass that
    # cannot possibly have run must not print the same line as one that ran
    # and found everything correct.
    if not cooled_racks:
        run.planned_updates += 1
        print("  .  no manifolds present yet -- wiring previews after the "
              "devices are created")
        return

    changed = 0
    unresolved = []

    def wire(intake, outflow, label):
        """Point an intake at the outflow that supplies it."""
        nonlocal changed
        if intake is None or outflow is None:
            unresolved.append(label)
            return
        current = getattr(intake.cooling_outflow, "id", None)
        if current == outflow.id:
            run.existing += 1
            return
        changed += 1
        if run.dry_run:
            run.planned_updates += 1
            return
        intake.update({"cooling_outflow": outflow.id})
        run.updated += 1

    for index, rack_name in enumerate(cooled_racks):
        manifold = devices.get(f"manifold-{rack_name.lower()}")
        cdu = devices.get(f"cdu-{index // racks_per_cdu + 1:02d}")
        if manifold is None or cdu is None:
            unresolved.append(f"manifold/cdu for {rack_name}")
            continue

        # CDU outflow -> this rack's manifold intake.
        port = index % racks_per_cdu + 1
        wire(
            intakes.get(manifold.id, {}).get("tcs-in"),
            outflows.get(cdu.id, {}).get(f"tcs-out{port}"),
            f"{manifold.name}/tcs-in",
        )

        # Manifold drops -> each liquid-cooled tray in the same rack. Ordered
        # by device name so the mapping is stable across runs, the same
        # determinism rule the power outlet allocator follows.
        trays = sorted(
            [
                d for d in devices.values()
                if d.rack and d.rack.id == manifold.rack.id
                and "cold-plate" in intakes.get(d.id, {})
            ],
            key=lambda d: natural_key(d.name),
        )
        for n, tray in enumerate(trays, start=1):
            wire(
                intakes.get(tray.id, {}).get("cold-plate"),
                outflows.get(manifold.id, {}).get(f"drop{n}"),
                f"{tray.name}/cold-plate",
            )

    if unresolved:
        run.skipped += len(unresolved)
        print(f"  .  {len(unresolved)} intakes unresolved, "
              f"e.g. {unresolved[0]}")
    if not changed:
        print("  =  all cooling intakes already wired")
    elif run.dry_run:
        print(f"  ~  {changed} intakes would be wired to their upstream outflow")
    else:
        print(f"  ~  {changed} intakes wired to their upstream outflow")


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
        # Only written when declared. An undeclared cooling_method is "not
        # stated", which is different from "air" -- and defaulting it would
        # silently assert something about every device type in the air-cooled
        # pod that its spec never claimed.
        if "cooling_method" in dt_spec:
            payload["cooling_method"] = dt_spec["cooling_method"]

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
                    "_parent": po["power_port"],
                })
        if outlets:
            ensure_parented(
                run, device_type, outlets, "power outlets",
                run.nb.dcim.power_port_templates,
                run.nb.dcim.power_outlet_templates,
                "power_port",
            )

        # Cooling intakes carry no parent -- an intake's upstream outflow sits
        # on a DIFFERENT device, which a device type cannot express. That
        # asymmetry is in NetBox itself: cooling-outflow-templates have a
        # cooling_intake field and cooling-intake-templates have no
        # cooling_outflow, exactly as power-outlet-templates have power_port
        # and power-port-templates have nothing. Cross-device wiring happens
        # in build_cooling(), after the devices exist.
        intakes = []
        for ci in dt_spec.get("cooling_intakes", []):
            for name in expand(ci["name"]):
                entry = {"name": name}
                for field in ("type", "diameter", "diameter_unit",
                              "max_flow", "max_flow_unit", "description"):
                    if field in ci:
                        entry[field] = ci[field]
                intakes.append(entry)
        if intakes:
            ensure_components(
                run, run.nb.dcim.cooling_intake_templates,
                device_type, intakes, "cooling intakes",
            )

        outflows = []
        for co in dt_spec.get("cooling_outflows", []):
            for name in expand(co["name"]):
                entry = {"name": name, "_parent": co["cooling_intake"]}
                for field in ("type", "diameter", "diameter_unit",
                              "description"):
                    if field in co:
                        entry[field] = co[field]
                outflows.append(entry)
        if outflows:
            ensure_parented(
                run, device_type, outflows, "cooling outflows",
                run.nb.dcim.cooling_intake_templates,
                run.nb.dcim.cooling_outflow_templates,
                "cooling_intake",
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

    # One pod, one cabinet was true of the H100 model and is not true in
    # general: an NVL72 arrives as an integrated rack system while the CDU
    # rack beside it is an ordinary cabinet. `rack_types` is the list form and
    # `rack_type` the original singular; both are read so pod.yaml keeps
    # working unchanged.
    rack_type_specs = spec.get("rack_types")
    if not rack_type_specs:
        rack_type_specs = [spec["rack_type"]]

    print("\nmanufacturers")
    manufacturers = {}
    for rt in rack_type_specs:
        slug = rt["manufacturer_slug"]
        if slug in manufacturers:
            continue
        manufacturers[slug] = run.ensure(
            run.nb.dcim.manufacturers,
            {"slug": slug},
            {"name": rt["manufacturer"], "slug": slug},
            rt["manufacturer"],
        )

    print("\nrack types")
    rack_types = {}
    for rt in rack_type_specs:
        manufacturer = manufacturers[rt["manufacturer_slug"]]
        payload = {
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
        }
        # Cooling fields are optional and deliberately not defaulted. Setting
        # cooling_capability on a rack type that never declared one would
        # write a claim the spec does not make -- and would rewrite the
        # air-cooled pod's shared cabinet as a side effect of building this
        # one.
        for field in ("description", "cooling_capability", "cooling_capacity"):
            if field in rt:
                payload[field] = rt[field]

        rack_types[rt["slug"]] = run.ensure(
            run.nb.dcim.rack_types,
            {"slug": rt["slug"]},
            payload,
            rt["model"],
            depends_on=(manufacturer,),
        )

    default_rack_type = (
        list(rack_types.values())[0] if len(rack_types) == 1 else None
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
        rack_type = rack_types.get(rack_spec.get("type"), default_rack_type)

        payload = {
            "name": name,
            "site": site.id,
            "location": location.id,
            "rack_type": rack_type.id if rack_type else None,
            "role": role.id,
            "status": "active",
        }
        # No cooling fields here on purpose. A rack inherits them from its
        # type and NetBox discards a per-rack write, so setting them produced
        # an update that reported success and changed nothing. validate_spec()
        # now rejects them at the spec, which is the only place the mistake
        # can be caught before it becomes a permanent phantom diff.

        run.ensure(
            run.nb.dcim.racks,
            {"name": name, "site_id": site.id},
            payload,
            name,
            depends_on=(site, location, rack_type, role),
        )


# Every key this generator understands, by the block it appears in.
#
# WHY THIS EXISTS. Until phase 2 the generator silently ignored anything in
# the spec it did not read. Adding a whole `cooling:` section produced no
# objects and no complaint -- the run reported success and the model was not
# what the file said. That is the same class of failure as a get-or-create
# that never diffs attributes: the tool answers a question adjacent to the one
# being asked, and the report looks fine.
#
# A declarative spec makes a promise -- "this file is the desired end state" --
# and a generator that discards part of the file breaks that promise quietly.
# So an unrecognised key is an error, not a shrug.
SPEC_KEYS = {
    "top": {
        "meta", "region", "site", "location", "rack_type", "rack_types",
        "device_types", "rack_roles", "racks", "power_panels", "power_feeds",
        "power_cabling", "fabric_cabling", "device_roles", "device_groups",
        "cooling",
    },
    "rack_type": {
        "manufacturer", "manufacturer_slug", "model", "slug", "form_factor",
        "width", "u_height", "starting_unit", "outer_width", "outer_depth",
        "outer_unit", "max_weight", "weight_unit", "description",
        "cooling_capability", "cooling_capacity",
    },
    "rack": {
        "name", "role", "type", "cooling_capability", "cooling_capacity",
    },
    "device_type": {
        "manufacturer", "manufacturer_slug", "model", "slug", "u_height",
        "full_depth", "weight", "weight_unit", "airflow", "description",
        "cooling_method", "power_ports", "power_outlets", "interfaces",
        "cooling_intakes", "cooling_outflows",
    },
    "device_group": {
        "description", "device_type", "role", "racks", "per_rack",
        "name_template", "positions", "start_position", "position_step",
        "face",
    },
    "power_ports": {
        "name", "type", "maximum_draw", "allocated_draw", "description",
    },
    "power_outlets": {"name", "type", "power_port"},
    "interfaces": {"name", "type", "mgmt_only"},
    "cooling_intakes": {
        "name", "type", "diameter", "diameter_unit", "max_flow",
        "max_flow_unit", "description",
    },
    "cooling_outflows": {
        "name", "type", "diameter", "diameter_unit", "cooling_intake",
        "description",
    },
    "power_feeds": {
        "name_template", "panels", "letters", "count", "racks", "supply",
        "phase", "voltage", "amperage", "max_utilization", "type", "status",
    },
    "power_cabling": {
        "outlets_per_pdu", "topology", "shelf_type", "busbar_type",
        "shelves_per_rack", "trays_per_rack",
    },
    "cooling": {"sources", "feeds", "topology"},
    "cooling_sources": {
        "name", "type", "fluid_type", "cooling_capacity", "status",
        "description", "location",
    },
    "cooling_feeds": {
        "name", "source", "rack", "cooling_capacity", "max_flow",
        "max_flow_unit", "status", "description",
    },
    "cooling_topology": {"racks_per_cdu", "drops_per_rack", "liquid_fraction"},
}


class SpecError(RuntimeError):
    """Raised when the spec contains something the generator cannot honour."""


def check_keys(mapping, allowed, where, problems):
    """Record any key in `mapping` that this generator does not read."""
    if not isinstance(mapping, dict):
        return
    unknown = sorted(set(mapping) - SPEC_KEYS[allowed])
    for key in unknown:
        problems.append(f"{where}: unrecognised key {key!r}")


def validate_spec(spec):
    """Fail before writing anything if part of the spec would be ignored.

    Checks structure, not values -- NetBox rejects a bad slug with a clear 400
    of its own, and duplicating its choice lists here would just be a second
    thing to keep current. What NetBox cannot catch is a key this generator
    never looks at, because that never reaches NetBox at all.
    """
    problems = []
    check_keys(spec, "top", "spec", problems)

    for rt in spec.get("rack_types", []) or []:
        check_keys(rt, "rack_type", f"rack_types/{rt.get('slug', '?')}", problems)
    check_keys(spec.get("rack_type"), "rack_type", "rack_type", problems)

    for rack in spec.get("racks", []) or []:
        check_keys(rack, "rack", f"racks/{rack.get('name', '?')}", problems)

    for dt in spec.get("device_types", []) or []:
        label = dt.get("slug", "?")
        check_keys(dt, "device_type", f"device_types/{label}", problems)
        for block in ("power_ports", "power_outlets", "interfaces",
                      "cooling_intakes", "cooling_outflows"):
            for entry in dt.get(block, []) or []:
                check_keys(entry, block,
                           f"device_types/{label}/{block}/{entry.get('name', '?')}",
                           problems)

    for group in spec.get("device_groups", []) or []:
        check_keys(group, "device_group",
                   f"device_groups/{group.get('device_type', '?')}", problems)

    check_keys(spec.get("power_feeds"), "power_feeds", "power_feeds", problems)
    check_keys(spec.get("power_cabling"), "power_cabling", "power_cabling",
               problems)

    cooling = spec.get("cooling") or {}
    check_keys(cooling, "cooling", "cooling", problems)
    for source in cooling.get("sources", []) or []:
        check_keys(source, "cooling_sources",
                   f"cooling/sources/{source.get('name', '?')}", problems)
    for feed in cooling.get("feeds", []) or []:
        check_keys(feed, "cooling_feeds",
                   f"cooling/feeds/{feed.get('name', '?')}", problems)
    check_keys(cooling.get("topology"), "cooling_topology",
               "cooling/topology", problems)

    # Declaring a rack type nobody uses is harmless; naming one that does not
    # exist is not, and it fails much later and much less clearly.
    declared = {rt["slug"] for rt in spec.get("rack_types", []) or []}
    if spec.get("rack_type"):
        declared.add(spec["rack_type"]["slug"])
    for rack in spec.get("racks", []) or []:
        wanted = rack.get("type")
        if wanted and wanted not in declared:
            problems.append(
                f"racks/{rack.get('name', '?')}: type {wanted!r} is not "
                f"declared in rack_types"
            )
    if len(declared) > 1:
        for rack in spec.get("racks", []) or []:
            if not rack.get("type"):
                problems.append(
                    f"racks/{rack.get('name', '?')}: several rack types are "
                    f"declared, so this rack must name one"
                )

    # A rack whose cooling fields NetBox will throw away.
    #
    # cooling_capability and cooling_capacity are inherited from the rack type
    # when one is assigned, exactly as u_height and width are. NetBox accepts
    # a per-rack value, returns 200, and discards it -- so the reconciler sees
    # drift, "fixes" it, and finds the same drift on the next run, forever.
    #
    # This is the same class of problem as an unrecognised key, one level
    # down: a declaration that reaches NetBox and still has no effect. Caught
    # here for the same reason -- the spec claims to be the desired end state,
    # and a value that can never be reached does not belong in it.
    if declared:
        for rack in spec.get("racks", []) or []:
            ignored = sorted(
                {"cooling_capability", "cooling_capacity"} & set(rack)
            )
            if ignored:
                problems.append(
                    f"racks/{rack.get('name', '?')}: {', '.join(ignored)} "
                    f"cannot be set on a rack that has a rack type -- NetBox "
                    f"inherits these from the type and discards the write. "
                    f"Declare them on the rack type instead."
                )

    if problems:
        raise SpecError(
            f"{len(problems)} problem(s) in the spec:\n  "
            + "\n  ".join(problems)
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

    # Before connecting: a spec this generator cannot fully honour is a
    # failure now, not a surprise three hundred objects in.
    try:
        validate_spec(spec)
    except SpecError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

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
        build_cooling(run, spec)
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
