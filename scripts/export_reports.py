#!/usr/bin/env python3
"""
Generate the engineering exports for both pods from NetBox.

    exports/h100-power-report.csv      per-rack load, capacity, N-1 analysis
    exports/h100-cable-schedule.csv    every cable, from/to/type/length
    exports/h100-rail-map.csv          rail-optimised fabric verification
    exports/gb200-power-report.csv     as above, N+N redundancy
    exports/gb200-cable-schedule.csv
    exports/gb200-cooling-report.csv   cooling topology and derived heat load
    exports/pod-comparison.csv         the two designs side by side

NetBox's built-in CSV export dumps raw tables. None of these is a raw table:
the power report needs load rolled up per rack and evaluated against a feed
loss, the cable schedule needs both terminations resolved to device/port pairs
with their racks, and the comparison needs figures neither pod holds on its
own. That analysis is the deliverable.

EVERY REPORT IS SCOPED TO ONE SITE, AND CARRIES THE SITE AS A COLUMN.

That is not decoration. An earlier version of this script iterated every rack
in NetBox, which was correct while one pod existed and became silently wrong
the moment a second one did -- it would have written a merged power report
totalling 1.3 MW across two unrelated designs, with nothing in the file saying
so. NetBox's own UI shows both pods on the rack elevations page and hands you
a filter, which is right for a UI because a human is present to narrow it. A
report has no filter and no reader at the moment it runs, so it has to carry
its own scope. The filename says which pod; the column proves it.

Usage:
    python scripts/export_reports.py
    python scripts/export_reports.py --site reference-pod-2
"""

import argparse
import csv
import os
import sys

import yaml

from netbox_client import REPO_ROOT, ConfigError, connect

EXPORT_DIR = os.path.join(REPO_ROOT, "exports")
SPEC_DIR = os.path.join(REPO_ROOT, "spec")

# SOURCED: NVIDIA limits InfiniBand multimode optical to 50 m total travel and
# recommends staying under 30 m for optimum performance.
IB_OPTIMUM_M = 30
IB_MAX_M = 50

# SOURCED: 8 H100 per DGX H100 system; 4 Blackwell GPUs per GB200 compute
# tray. Kept as an explicit map rather than parsed out of a model name,
# because a report that infers GPU counts from strings will eventually infer
# one wrong and nothing will flag it.
GPUS_PER_DEVICE_TYPE = {
    "dgx-h100": 8,
    "gb200-compute-tray": 4,
}

# What each pod is and how its redundancy works.
#
# `redundancy` is the real difference. The H100 pod is N+1 across three
# sources, so losing one leaves n-1 carrying everything. The NVL72 is N+N
# across two sides, so losing one leaves n/2. Same PASS/FAIL column, different
# arithmetic -- named in its own column so nobody reads across the two
# reports assuming the verdicts mean the same thing.
SITES = [
    {
        "slug": "reference-pod-1",
        "prefix": "h100",
        "spec": "pod.yaml",
        "redundancy": "N+1",
        "cooling": False,
        # The rail map only means something where a rail-optimised fabric
        # exists. Declared per site rather than assumed, since the GB200 pod
        # has 4 rails per tray and no fabric cabling modelled yet.
        "rail_map": {
            "node_prefix": "dgx-",
            "rail_prefix": "ib-rail",
            "expected_nodes": 32,
        },
    },
    {
        "slug": "reference-pod-2",
        "prefix": "gb200",
        "spec": "pod-gb200.yaml",
        "redundancy": "N+N",
        "cooling": True,
        "rail_map": None,
    },
]


def draw(port):
    """Allocated draw in watts, treating an unset value as zero."""
    return getattr(port, "allocated_draw", None) or 0


def feed_capacity(feed):
    """Usable capacity of a power feed in VA.

    NetBox displays this but does not expose it through the API, so it is
    computed here -- which is better for a report anyway, since the formula is
    then visible rather than an opaque field.

        single phase: V x A x derate
        three phase:  V x A x 1.732 x derate

    For both pods: 415 x 60 x 1.732 x 0.80 = 34,501 VA.

    Note the constant is NetBox's three-decimal 1.732, not math.sqrt(3)
    (1.7320508...). Over 24,900 VA the fuller value yields 34,502 -- a
    difference of 1 VA that changes no conclusion, but would leave this report
    disagreeing with the system of record it exports from. Matching the source
    system is worth more here than the extra decimal places.

    Worth recording: NVIDIA's electrical table lists the same 415 V / 60 A
    three-phase circuit at 32.7 kW, implying a different derating assumption.
    Both figures are reported rather than silently choosing one.
    """
    voltage = feed.voltage or 0
    amperage = feed.amperage or 0
    derate = (feed.max_utilization or 100) / 100.0
    phase = getattr(feed.phase, "value", feed.phase)
    factor = 1.732 if phase == "three-phase" else 1.0
    return int(voltage * amperage * factor * derate)


def pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else 0.0


def load_spec(name):
    with open(os.path.join(SPEC_DIR, name)) as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# The power graph
#
# Everything below walks the actual cable path rather than dividing a rack's
# load by its feed count. The two agree in the H100 pod, where six PSUs spread
# evenly across three rPDUs. They do NOT agree in the NVL72, where 27 trays of
# two different sizes land on 8 power shelves and the result is a 13,300 to
# 16,200 VA spread. Assuming even division would have hidden that, and the
# spread is the finding.


def power_graph(nb):
    """Index the power topology once: outlets by parent port, and what each
    outlet and feed is cabled to.

    Returns (outlets_by_port, port_of_outlet, port_of_feed).
    """
    ports = list(nb.dcim.power_ports.all())
    port_by_id = {p.id: p for p in ports}

    outlets_by_port = {}
    for outlet in nb.dcim.power_outlets.all():
        # An outlet with no parent power port is a dead end -- there is no
        # path from it up to a feed. That was the silent S3 failure, and it
        # would show up here as a feed reading less than its rack's load.
        if outlet.power_port:
            outlets_by_port.setdefault(outlet.power_port.id, []).append(outlet)

    port_of_outlet = {}
    port_of_feed = {}
    for cable in nb.dcim.cables.all():
        ends = {}
        for side in (cable.a_terminations, cable.b_terminations):
            if not side:
                continue
            term = side[0]
            ends[getattr(term, "object_type", "")] = getattr(term, "object", None)

        port = ends.get("dcim.powerport")
        if port is None:
            continue
        if ends.get("dcim.poweroutlet") is not None:
            port_of_outlet[ends["dcim.poweroutlet"].id] = port_by_id.get(port.id)
        if ends.get("dcim.powerfeed") is not None:
            port_of_feed[ends["dcim.powerfeed"].id] = port_by_id.get(port.id)

    return outlets_by_port, port_of_outlet, port_of_feed


def port_load(port, outlets_by_port, port_of_outlet, seen=None):
    """Watts drawn through a power port, following pass-throughs downstream.

    A port that declares a draw IS the load -- that is a device's own supply.
    A port that declares none is a pass-through, and its load is whatever is
    plugged into the outlets parented to it.

    That rule is NetBox's, not ours, and it is why the rPDU and busbar input
    ports carry explicit nulls in both specs: declaring a value on a
    pass-through makes NetBox use the declared number instead of computing,
    and the feed then reads a plausible figure that has nothing to do with
    what is connected.

    Recursion depth is two hops for the H100 chain (rPDU -> PSU) and three for
    the NVL72 (shelf -> busbar -> tray). `seen` guards against a cabling loop
    rather than trusting that nobody ever creates one.
    """
    if port is None:
        return 0

    declared = draw(port)
    if declared:
        return declared

    seen = seen if seen is not None else set()
    if port.id in seen:
        return 0
    seen.add(port.id)

    total = 0
    for outlet in outlets_by_port.get(port.id, []):
        total += port_load(
            port_of_outlet.get(outlet.id), outlets_by_port, port_of_outlet, seen
        )
    return total


def site_index(nb, slug):
    """Racks, devices and feeds belonging to one site."""
    site = nb.dcim.sites.get(slug=slug)
    if site is None:
        return None

    racks = {r.id: r for r in nb.dcim.racks.all() if r.site and r.site.id == site.id}
    devices = [
        d for d in nb.dcim.devices.all()
        if d.rack and d.rack.id in racks
    ]
    ports_by_device = {}
    for p in nb.dcim.power_ports.all():
        if p.device.id in {d.id for d in devices}:
            ports_by_device.setdefault(p.device.id, []).append(p)

    feeds_by_rack = {}
    for f in nb.dcim.power_feeds.all():
        if f.rack and f.rack.id in racks:
            feeds_by_rack.setdefault(f.rack.id, []).append(f)

    u_height = {dt.id: dt.u_height for dt in nb.dcim.device_types.all()}

    return {
        "site": site,
        "racks": racks,
        "devices": devices,
        "ports_by_device": ports_by_device,
        "feeds_by_rack": feeds_by_rack,
        "u_height": u_height,
    }


def survivors(count, scheme):
    """Feeds still carrying load after the design's worst credible loss.

    N+1 across three sources loses one: two survive.
    N+N across two sides loses a side: half survive.
    """
    if count < 2:
        return 0
    if scheme == "N+N":
        return count // 2
    return count - 1


def power_report(nb, cfg, index, graph, path):
    outlets_by_port, port_of_outlet, port_of_feed = graph
    site_name = index["site"].name
    scheme = cfg["redundancy"]

    rows = []
    for rack in sorted(index["racks"].values(), key=lambda r: r.name):
        in_rack = [d for d in index["devices"] if d.rack.id == rack.id]

        # Every port in the rack, pass-throughs included.
        #
        # An earlier version excluded rPDUs by name prefix to avoid
        # double-counting. It never needed to: a pass-through port declares no
        # draw and contributes zero, which is the same mechanism that makes
        # the walk below work. All the prefix did was drop 36 devices and 72U
        # out of the counts, so the CSV reported 56 devices while the README
        # reported 92. The README was right.
        allocated = sum(
            draw(p) for d in in_rack for p in index["ports_by_device"].get(d.id, [])
        )
        used_u = sum(
            index["u_height"].get(d.device_type.id, 0) or 0 for d in in_rack
        )

        feeds = sorted(index["feeds_by_rack"].get(rack.id, []), key=lambda f: f.name)
        n = len(feeds)

        # Walked, not divided. min() rather than [0] because a rack whose
        # feeds differ in rating should be judged by its weakest one, not by
        # whichever happens to sort first.
        capacity = min((feed_capacity(f) for f in feeds), default=0)
        feed_loads = [
            port_load(port_of_feed.get(f.id), outlets_by_port, port_of_outlet)
            for f in feeds
        ]
        carried = sum(feed_loads)
        worst_feed = max(feed_loads, default=0)

        alive = survivors(n, scheme)
        loss_load = carried / alive if alive else 0

        if n == 0:
            verdict = "NO FEEDS"
        elif alive == 0:
            verdict = "NO REDUNDANCY"
        else:
            verdict = "PASS" if loss_load <= capacity else "FAIL"

        rows.append({
            "site": site_name,
            "rack": rack.name,
            "role": rack.role.name if rack.role else "",
            "devices": len(in_rack),
            "u_used": used_u,
            "u_total": rack.u_height,
            "feeds": n,
            "feed_capacity_va": capacity,
            "allocated_w": allocated,
            # The difference between what the rack draws and what its feeds
            # actually carry. Zero means every supply has a path to a feed.
            # Anything else is equipment drawing power that no circuit is
            # modelled as providing -- deliberate or not, it should be a
            # number in the report rather than a sentence in a comment.
            "uncabled_w": allocated - carried,
            "worst_feed_w": worst_feed,
            "worst_feed_pct": pct(worst_feed, capacity),
            "redundancy": scheme,
            "surviving_feeds": alive,
            "loss_per_feed_w": round(loss_load),
            "loss_util_pct": pct(loss_load, capacity),
            "verdict": verdict,
        })

    verdicts = {r["verdict"] for r in rows}
    rows.append({
        "site": site_name,
        "rack": "SITE TOTAL",
        "role": "",
        "devices": sum(r["devices"] for r in rows),
        "u_used": sum(r["u_used"] for r in rows),
        "u_total": sum(r["u_total"] for r in rows),
        "feeds": sum(r["feeds"] for r in rows),
        "feed_capacity_va": "",
        "allocated_w": sum(r["allocated_w"] for r in rows),
        "uncabled_w": sum(r["uncabled_w"] for r in rows),
        "worst_feed_w": max((r["worst_feed_w"] for r in rows), default=0),
        "worst_feed_pct": max((r["worst_feed_pct"] for r in rows), default=0),
        "redundancy": scheme,
        "surviving_feeds": "",
        "loss_per_feed_w": "",
        "loss_util_pct": max((r["loss_util_pct"] for r in rows), default=0),
        # A total that reads PASS while one rack has no feeds modelled would
        # be worse than useless. Any non-PASS row propagates.
        "verdict": "PASS" if verdicts == {"PASS"} else "SEE ROWS",
    })

    write_csv(path, rows)
    return rows


def termination(term, device_rack):
    """Resolve one cable termination to (device, port, rack).

    A termination is usually a device component, but a power feed terminates
    directly on the feed object -- which has a rack but no device.
    """
    obj = getattr(term, "object", None)
    if obj is None:
        return ("", "", "")

    device = getattr(obj, "device", None)
    if device is not None:
        return (
            getattr(device, "name", ""),
            getattr(obj, "name", ""),
            device_rack.get(device.id, ""),
        )

    rack = getattr(obj, "rack", None)
    return (
        "(power feed)",
        getattr(obj, "name", ""),
        getattr(rack, "name", "") if rack else "",
    )


def cable_schedule(nb, cfg, index, path):
    site_name = index["site"].name
    device_rack = {d.id: d.rack.name for d in index["devices"]}
    our_devices = set(device_rack)
    our_feeds = {
        f.id for feeds in index["feeds_by_rack"].values() for f in feeds
    }

    def belongs(cable):
        """Whether this cable is part of this site.

        Checked per termination rather than per cable id, because nothing in a
        cable itself names a site -- it is inferred from what it connects.
        """
        for side in (cable.a_terminations, cable.b_terminations):
            if not side:
                continue
            obj = getattr(side[0], "object", None)
            if obj is None:
                continue
            device = getattr(obj, "device", None)
            if device is not None and device.id in our_devices:
                return True
            if getattr(side[0], "object_type", "") == "dcim.powerfeed" \
                    and obj.id in our_feeds:
                return True
        return False

    rows = []
    for cable in nb.dcim.cables.all():
        if not belongs(cable):
            continue

        a_dev, a_port, a_rack = termination(
            cable.a_terminations[0] if cable.a_terminations else None, device_rack
        )
        b_dev, b_port, b_rack = termination(
            cable.b_terminations[0] if cable.b_terminations else None, device_rack
        )

        length = cable.length
        cable_type = getattr(cable.type, "value", cable.type) or ""

        # Only optical InfiniBand is subject to the reach guidance.
        reach = ""
        if length and "mmf" in str(cable_type):
            if length > IB_MAX_M:
                reach = "OVER 50m LIMIT"
            elif length > IB_OPTIMUM_M:
                reach = "over 30m optimum"
            else:
                reach = "ok"

        rows.append({
            "site": site_name,
            "cable_id": cable.id,
            "type": cable_type,
            "length_m": length or "",
            "a_rack": a_rack,
            "a_device": a_dev,
            "a_termination": a_port,
            "b_rack": b_rack,
            "b_device": b_dev,
            "b_termination": b_port,
            "reach_check": reach,
        })

    rows.sort(key=lambda r: (r["a_rack"], r["a_device"], r["a_termination"]))
    write_csv(path, rows)
    return rows


def rail_map(nb, cfg, index, path):
    """Prove the rail-optimised property in eight rows.

    For each rail, which leaf its cables land on and how many distinct nodes
    reach it. A correct fabric shows exactly one leaf per rail and 32 nodes on
    each. Anything else -- two leaves for one rail, or a node count below 32 --
    is a miscabling that still links and still passes a port count.
    """
    spec = cfg["rail_map"]
    if not spec:
        return None

    site_name = index["site"].name
    our_devices = {d.id for d in index["devices"]}

    device_of = {}
    for iface in nb.dcim.interfaces.all():
        if iface.device.id in our_devices:
            device_of[iface.id] = (iface.device.name, iface.name)

    rails = {}
    for cable in nb.dcim.cables.all():
        ends = []
        for side in (cable.a_terminations, cable.b_terminations):
            if not side:
                continue
            term = side[0]
            # MUST filter on object_type. Interfaces, power ports, power
            # outlets and power feeds are separate tables with independent id
            # sequences, so power outlet #100 will happily match interface
            # #100 in this lookup and report a DGX node as a leaf switch.
            if getattr(term, "object_type", None) != "dcim.interface":
                continue
            obj = getattr(term, "object", None)
            if obj is not None and obj.id in device_of:
                ends.append(device_of[obj.id])
        if len(ends) != 2:
            continue

        for (dev_a, port_a), (dev_b, _) in (ends, ends[::-1]):
            if dev_a.startswith(spec["node_prefix"]) \
                    and port_a.startswith(spec["rail_prefix"]):
                rail = port_a.replace(spec["rail_prefix"], "")
                entry = rails.setdefault(rail, {"leaves": set(), "nodes": set()})
                entry["leaves"].add(dev_b)
                entry["nodes"].add(dev_a)

    rows = []
    for rail in sorted(rails, key=int):
        entry = rails[rail]
        rows.append({
            "site": site_name,
            "rail": rail,
            "leaf": ", ".join(sorted(entry["leaves"])),
            "leaf_count": len(entry["leaves"]),
            "nodes": len(entry["nodes"]),
            # PASS/FAIL, matching the power report. An earlier version used
            # "CHECK", which reads as easily as "checked, fine" as it does as
            # "look at this" -- a status that needs interpreting is not a
            # status.
            "verdict": "PASS" if len(entry["leaves"]) == 1
                       and len(entry["nodes"]) == spec["expected_nodes"]
                       else "FAIL",
        })

    if not rows:
        return None
    write_csv(path, rows)
    return rows


def cooling_report(nb, cfg, index, power_rows, path):
    """The cooling topology, and a heat load that NetBox did not compute.

    READ THE `basis` COLUMN BEFORE USING ANY NUMBER IN HERE.

    NetBox 4.7's cooling model carries capacity on the source, the feed and
    the rack, and demand nowhere: a cooling intake has no allocated_draw
    equivalent, and there is no cable or foreign key joining a feed to a
    device component. So unlike the power report -- where the load column is
    walked out of cabling NetBox already holds -- every load figure below is
    arithmetic this script performs on the power model.

    That distinction is the whole reason the column exists. In S6 the N-1
    verdict counted as independent reproduction of NVIDIA's 20.4 kW because
    NetBox derived it and we only read it off. Nothing here has that standing,
    and presenting it as though it did would be the same mistake in a new
    domain.
    """
    site_name = index["site"].name
    spec = load_spec(cfg["spec"])

    # ASSUMED, and declared in the spec rather than here so that one number
    # does not end up living in two files and quietly diverging.
    topology = (spec.get("cooling") or {}).get("topology") or {}
    liquid_fraction = topology.get("liquid_fraction")

    site_id = index["site"].id
    sources = [
        s for s in nb.dcim.cooling_sources.all() if s.site and s.site.id == site_id
    ]
    our_sources = {s.id for s in sources}
    site_feeds = [
        f for f in nb.dcim.cooling_feeds.all()
        if f.cooling_source and f.cooling_source.id in our_sources
    ]

    feeds_by_rack = {}
    for feed in site_feeds:
        if feed.rack:
            feeds_by_rack.setdefault(feed.rack.id, []).append(feed)

    our_devices = {d.id: d for d in index["devices"]}
    intakes_by_rack = {}
    wired_by_rack = {}
    for intake in nb.dcim.cooling_intakes.all():
        device = our_devices.get(intake.device.id)
        if device is None:
            continue
        intakes_by_rack[device.rack.id] = intakes_by_rack.get(device.rack.id, 0) + 1
        if intake.cooling_outflow:
            wired_by_rack[device.rack.id] = wired_by_rack.get(device.rack.id, 0) + 1

    load_by_rack = {r["rack"]: r["allocated_w"] for r in power_rows}

    rows = []
    for rack in sorted(index["racks"].values(), key=lambda r: r.name):
        feeds = feeds_by_rack.get(rack.id, [])
        load_kw = load_by_rack.get(rack.name, 0) / 1000.0
        capability = getattr(rack.cooling_capability, "value", rack.cooling_capability)

        # The liquid fraction applies to racks that HAVE a liquid loop. An
        # air-only rack rejects all of its heat to the room whatever the pod's
        # headline split says -- applying 85% to the CDU rack would have
        # invented 15 kW of liquid load out of pump and electrical losses that
        # nothing in this design cools with water.
        if capability in ("hybrid", "liquid-only") and liquid_fraction:
            liquid = round(load_kw * liquid_fraction, 1)
            air = round(load_kw - liquid, 1)
        else:
            liquid = 0.0
            air = round(load_kw, 1)
        capacity = float(rack.cooling_capacity) if rack.cooling_capacity else 0.0
        intakes = intakes_by_rack.get(rack.id, 0)
        wired = wired_by_rack.get(rack.id, 0)

        # An unwired intake is not automatically a defect.
        #
        # A rack served by a CoolingFeed sits at the plant boundary, and an
        # intake there takes facility water. NetBox has no foreign key from a
        # feed to an intake and no cable for coolant, so that link CANNOT be
        # expressed -- the CDUs' fws-in intakes will read unwired forever.
        #
        # Reported in its own column rather than folded into a total, because
        # a count that is permanently non-zero and unexplained is one readers
        # learn to skip. What remains in `unwired_unexplained` is the number
        # that should always be zero, and a verdict is allowed to depend on.
        at_plant = min(intakes - wired, intakes) if feeds else 0
        unexplained = intakes - wired - at_plant

        if unexplained:
            verdict = "UNWIRED INTAKES"
        elif not capacity:
            verdict = "NO CAPACITY SET"
        else:
            verdict = "PASS" if load_kw <= capacity else "FAIL"

        rows.append({
            "site": site_name,
            "rack": rack.name,
            "cooling_capability": capability or "",
            "rack_capacity_kw": capacity or "",
            "cooling_feeds": ", ".join(f.name for f in feeds),
            "cooling_source": ", ".join(
                f.cooling_source.name for f in feeds if f.cooling_source
            ),
            "intakes": intakes,
            # An intake with no upstream outflow is plumbed to nothing. It is
            # the cooling analogue of an outlet with no parent power port, and
            # it fails just as silently -- nothing in NetBox computes across
            # this link, so nothing would notice.
            "intakes_wired": wired,
            "unwired_at_plant": at_plant,
            "unwired_unexplained": unexplained,
            "total_load_kw": round(load_kw, 1),
            "liquid_load_kw": liquid,
            "air_load_kw": air,
            "basis": "derived from power model, not computed by NetBox",
            "verdict": verdict,
        })

    total_load = sum(r["total_load_kw"] for r in rows)
    total_liquid = sum(r["liquid_load_kw"] for r in rows)
    plant_kw = sum(float(s.cooling_capacity or 0) for s in sources)

    rows.append({
        "site": site_name,
        "rack": "SITE TOTAL",
        "cooling_capability": "",
        "rack_capacity_kw": "",
        "cooling_feeds": f"{len(site_feeds)} feeds",
        "cooling_source": f"{len(sources)} sources, {plant_kw:.0f} kW installed",
        "intakes": sum(r["intakes"] for r in rows),
        "intakes_wired": sum(r["intakes_wired"] for r in rows),
        "unwired_at_plant": sum(r["unwired_at_plant"] for r in rows),
        "unwired_unexplained": sum(r["unwired_unexplained"] for r in rows),
        "total_load_kw": round(total_load, 1),
        "liquid_load_kw": round(total_liquid, 1),
        "air_load_kw": round(total_load - total_liquid, 1),
        "basis": "derived from power model, not computed by NetBox",
        # Plant is sized against the liquid load only; the air fraction is
        # rejected by room cooling, which this model does not represent.
        "verdict": (
            "UNWIRED INTAKES"
            if any(r["unwired_unexplained"] for r in rows)
            else "PASS" if plant_kw >= total_liquid else "FAIL"
        ),
    })

    write_csv(path, rows)
    return rows


def gpu_count(devices):
    return sum(
        GPUS_PER_DEVICE_TYPE.get(d.device_type.slug, 0) for d in devices
    )


def comparison(results, path):
    """The two designs side by side.

    Compute racks only. The GB200 pod has no network or management racks
    modelled yet -- the compute fabric switch counts per scalable unit and the
    SN5600 specifications are not sourced -- so a whole-pod rack count would
    flatter it by comparing eight racks against twelve. Stated in the file
    rather than left for a reader to notice.
    """
    def stats(result):
        index, rows, cfg = result
        racks = index["racks"].values()
        compute = [r for r in racks if r.role and r.role.name == "Compute"]
        compute_names = {r.name for r in compute}
        compute_rows = [r for r in rows if r["rack"] in compute_names]
        devices = index["devices"]
        gpus = gpu_count(devices)
        load_w = sum(r["allocated_w"] for r in compute_rows)
        return {
            "cfg": cfg,
            "racks": len(racks),
            "compute_racks": len(compute),
            "gpus": gpus,
            "devices": len(devices),
            "compute_load_kw": load_w / 1000.0,
            "feeds_per_rack": (
                compute_rows[0]["feeds"] if compute_rows else 0
            ),
            "worst_loss_pct": max(
                (r["loss_util_pct"] for r in compute_rows), default=0
            ),
            "cooling": "hybrid liquid + air" if cfg["cooling"] else "air",
        }

    a, b = (stats(r) for r in results)

    def ratio(x, y):
        return round(y / x, 2) if x else ""

    def per_1000(count, gpus):
        return round(1000.0 * count / gpus, 1) if gpus else ""

    metrics = [
        ("GPUs", a["gpus"], b["gpus"], ratio(a["gpus"], b["gpus"]), ""),
        ("Compute racks", a["compute_racks"], b["compute_racks"],
         ratio(a["compute_racks"], b["compute_racks"]), ""),
        ("GPUs per compute rack",
         round(a["gpus"] / a["compute_racks"], 1),
         round(b["gpus"] / b["compute_racks"], 1),
         ratio(a["gpus"] / a["compute_racks"], b["gpus"] / b["compute_racks"]), ""),
        ("kW per compute rack",
         round(a["compute_load_kw"] / a["compute_racks"], 1),
         round(b["compute_load_kw"] / b["compute_racks"], 1),
         ratio(a["compute_load_kw"] / a["compute_racks"],
               b["compute_load_kw"] / b["compute_racks"]), ""),
        ("kW per GPU",
         round(a["compute_load_kw"] / a["gpus"], 3),
         round(b["compute_load_kw"] / b["gpus"], 3),
         ratio(a["compute_load_kw"] / a["gpus"], b["compute_load_kw"] / b["gpus"]),
         "the density trade, stated plainly"),
        ("Compute racks per 1,000 GPUs",
         per_1000(a["compute_racks"], a["gpus"]),
         per_1000(b["compute_racks"], b["gpus"]),
         ratio(per_1000(a["compute_racks"], a["gpus"]),
               per_1000(b["compute_racks"], b["gpus"])), ""),
        ("kW per 1,000 GPUs",
         round(1000 * a["compute_load_kw"] / a["gpus"]),
         round(1000 * b["compute_load_kw"] / b["gpus"]),
         ratio(a["compute_load_kw"] / a["gpus"], b["compute_load_kw"] / b["gpus"]),
         ""),
        ("Compute load, kW",
         round(a["compute_load_kw"], 1), round(b["compute_load_kw"], 1),
         ratio(a["compute_load_kw"], b["compute_load_kw"]), ""),
        ("Devices modelled", a["devices"], b["devices"],
         ratio(a["devices"], b["devices"]), ""),
        ("Feeds per compute rack", a["feeds_per_rack"], b["feeds_per_rack"],
         ratio(a["feeds_per_rack"], b["feeds_per_rack"]), ""),
        ("Redundancy scheme", a["cfg"]["redundancy"], b["cfg"]["redundancy"], "",
         "N+1 loses one of three; N+N loses one of two sides"),
        ("Worst surviving feed, %", a["worst_loss_pct"], b["worst_loss_pct"],
         ratio(a["worst_loss_pct"], b["worst_loss_pct"]),
         "headroom after the worst credible loss"),
        ("Cooling", a["cooling"], b["cooling"], "",
         "GB200 liquid figures are derived, not computed by NetBox"),
    ]

    rows = [
        {
            "metric": name,
            a["cfg"]["prefix"]: x,
            b["cfg"]["prefix"]: y,
            "ratio": r,
            "note": note,
        }
        for name, x, y, r, note in metrics
    ]
    write_csv(path, rows)
    return rows


def write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--site",
        help="export one site only (slug); default is every site",
    )
    args = parser.parse_args()

    try:
        nb, info = connect()
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    os.makedirs(EXPORT_DIR, exist_ok=True)
    print(f"{info['url']}\n")

    graph = power_graph(nb)
    results = []

    for cfg in SITES:
        if args.site and cfg["slug"] != args.site:
            continue

        index = site_index(nb, cfg["slug"])
        if index is None:
            print(f"site {cfg['slug']} not found -- skipped\n")
            continue

        prefix = cfg["prefix"]
        print(f"=== {index['site'].name} ({prefix}) ===")

        power_path = os.path.join(EXPORT_DIR, f"{prefix}-power-report.csv")
        rows = power_report(nb, cfg, index, graph, power_path)
        total = rows[-1]
        print(f"wrote {power_path}")
        print(f"  racks:            {len(rows) - 1}")
        print(f"  site load:        {total['allocated_w'] / 1000:.1f} kW")
        if total["uncabled_w"]:
            print(f"  UNCABLED:         {total['uncabled_w']} W "
                  f"drawn with no feed path")
        print(f"  redundancy:       {cfg['redundancy']}")
        print(f"  verdict:          {total['verdict']}")
        worst = max(
            (r for r in rows[:-1] if r["feeds"]),
            key=lambda r: r["loss_util_pct"],
            default=None,
        )
        if worst:
            print(f"  worst after loss: {worst['rack']} at "
                  f"{worst['loss_util_pct']}%")

        cable_path = os.path.join(EXPORT_DIR, f"{prefix}-cable-schedule.csv")
        cables = cable_schedule(nb, cfg, index, cable_path)
        print(f"\nwrote {cable_path}")
        print(f"  cables:           {len(cables)}")
        over = [c for c in cables if c["reach_check"].startswith("over")]
        limit = [c for c in cables if c["reach_check"].startswith("OVER")]
        print(f"  over 30m optimum: {len(over)}")
        print(f"  over 50m limit:   {len(limit)}")

        rail_path = os.path.join(EXPORT_DIR, f"{prefix}-rail-map.csv")
        rails = rail_map(nb, cfg, index, rail_path)
        if rails:
            print(f"\nwrote {rail_path}")
            for r in rails:
                print(f"  rail {r['rail']} -> {r['leaf']}  "
                      f"({r['nodes']} nodes)  {r['verdict']}")

        if cfg["cooling"]:
            cool_path = os.path.join(EXPORT_DIR, f"{prefix}-cooling-report.csv")
            cooling = cooling_report(nb, cfg, index, rows, cool_path)
            cool_total = cooling[-1]
            print(f"\nwrote {cool_path}")
            print(f"  total load:       {cool_total['total_load_kw']} kW")
            print(f"  liquid (derived): {cool_total['liquid_load_kw']} kW")
            print(f"  air (derived):    {cool_total['air_load_kw']} kW")
            print(f"  intakes wired:    {cool_total['intakes_wired']} "
                  f"of {cool_total['intakes']}  "
                  f"({cool_total['unwired_at_plant']} at plant boundary, "
                  f"{cool_total['unwired_unexplained']} unexplained)")
            print(f"  plant verdict:    {cool_total['verdict']}")

        print()
        results.append((index, rows, cfg))

    if len(results) == 2:
        comp_path = os.path.join(EXPORT_DIR, "pod-comparison.csv")
        comparison(results, comp_path)
        print(f"wrote {comp_path}")
    elif not args.site:
        print("comparison needs both sites -- skipped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
