#!/usr/bin/env python3
"""
Generate the pod's engineering exports from NetBox.

    exports/power-report.csv     per-rack load, capacity, and N-1 analysis
    exports/cable-schedule.csv   every cable, from/to/type/length

NetBox's built-in CSV export dumps raw tables. Neither of these reports is a
raw table: the power report needs the load rolled up per rack and evaluated
against a feed loss, and the cable schedule needs both terminations resolved
to device/port pairs with their racks. That analysis is the deliverable.

Usage:
    python scripts/export_reports.py
"""

import csv
import os
import sys

from netbox_client import REPO_ROOT, ConfigError, connect

EXPORT_DIR = os.path.join(REPO_ROOT, "exports")

# SOURCED: NVIDIA limits InfiniBand multimode optical to 50 m total travel and
# recommends staying under 30 m for optimum performance.
IB_OPTIMUM_M = 30
IB_MAX_M = 50


def draw(port):
    """Allocated draw in watts, treating an unset value as zero."""
    return getattr(port, "allocated_draw", None) or 0


def feed_capacity(feed):
    """Usable capacity of a power feed in VA.

    NetBox displays this but does not expose it through the API, so it is
    computed here -- which is better for a report anyway, since the formula
    is then visible rather than an opaque field.

        single phase: V x A x derate
        three phase:  V x A x 1.732 x derate

    For this pod: 415 x 60 x 1.732 x 0.80 = 34,501 VA.

    Note the constant is NetBox's three-decimal 1.732, not math.sqrt(3)
    (1.7320508...). Over 24,900 VA the fuller value yields 34,502 -- a
    difference of 1 VA that changes no conclusion, but would leave this
    report disagreeing with the system of record it exports from. Matching
    the source system is worth more here than the extra decimal places.

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


def build_indexes(nb):
    racks = {r.id: r for r in nb.dcim.racks.all()}
    devices = list(nb.dcim.devices.all())
    device_by_id = {d.id: d for d in devices}
    u_height = {dt.id: dt.u_height for dt in nb.dcim.device_types.all()}

    ports_by_device = {}
    for p in nb.dcim.power_ports.all():
        ports_by_device.setdefault(p.device.id, []).append(p)

    feeds_by_rack = {}
    for f in nb.dcim.power_feeds.all():
        if f.rack:
            feeds_by_rack.setdefault(f.rack.id, []).append(f)

    return racks, devices, device_by_id, u_height, ports_by_device, feeds_by_rack


def power_report(nb, path):
    racks, devices, _, u_height, ports_by_device, feeds_by_rack = build_indexes(nb)

    rows = []
    for rack in sorted(racks.values(), key=lambda r: r.name):
        in_rack = [d for d in devices if d.rack and d.rack.id == rack.id]

        # rPDUs are pass-through: counting their ports would double-count the
        # load already accounted for by whatever is plugged into them.
        powered = [d for d in in_rack if not d.name.startswith("pdu-")]

        allocated = sum(
            draw(p) for d in powered for p in ports_by_device.get(d.id, [])
        )
        used_u = sum(u_height.get(d.device_type.id, 0) or 0 for d in powered)

        feeds = feeds_by_rack.get(rack.id, [])
        per_feed_capacity = feed_capacity(feeds[0]) if feeds else 0
        n = len(feeds)

        per_feed_load = allocated / n if n else 0
        # N-1: one source lost, the survivors carry the whole rack.
        n1_load = allocated / (n - 1) if n > 1 else allocated

        rows.append({
            "rack": rack.name,
            "role": rack.role.name if rack.role else "",
            "devices": len(powered),
            "u_used": used_u,
            "u_total": rack.u_height,
            "feeds": n,
            "feed_capacity_va": per_feed_capacity,
            "allocated_w": allocated,
            "per_feed_w": round(per_feed_load),
            "util_pct": pct(per_feed_load, per_feed_capacity),
            "n1_per_feed_w": round(n1_load),
            "n1_util_pct": pct(n1_load, per_feed_capacity),
            "n1_ok": "PASS" if n1_load <= per_feed_capacity else "FAIL",
        })

    total_allocated = sum(r["allocated_w"] for r in rows)
    rows.append({
        "rack": "POD TOTAL",
        "role": "",
        "devices": sum(r["devices"] for r in rows),
        "u_used": sum(r["u_used"] for r in rows),
        "u_total": sum(r["u_total"] for r in rows),
        "feeds": sum(r["feeds"] for r in rows),
        "feed_capacity_va": "",
        "allocated_w": total_allocated,
        "per_feed_w": "",
        "util_pct": "",
        "n1_per_feed_w": "",
        "n1_util_pct": "",
        "n1_ok": "PASS" if all(r["n1_ok"] == "PASS" for r in rows) else "FAIL",
    })

    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

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


def cable_schedule(nb, path):
    device_rack = {
        d.id: (d.rack.name if d.rack else "") for d in nb.dcim.devices.all()
    }

    rows = []
    for cable in nb.dcim.cables.all():
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

    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return rows


def rail_map(nb, path):
    """Prove the rail-optimised property in eight rows.

    For each rail, which leaf its cables land on and how many distinct nodes
    reach it. A correct fabric shows exactly one leaf per rail and 32 nodes
    on each. Anything else -- two leaves for one rail, or a node count below
    32 -- is a miscabling that still links and still passes a port count.
    """
    device_of = {}
    for iface in nb.dcim.interfaces.all():
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

        for (dev_a, port_a), (dev_b, port_b) in (ends, ends[::-1]):
            if dev_a.startswith("dgx-") and port_a.startswith("ib-rail"):
                rail = port_a.replace("ib-rail", "")
                entry = rails.setdefault(rail, {"leaves": set(), "nodes": set()})
                entry["leaves"].add(dev_b)
                entry["nodes"].add(dev_a)

    rows = []
    for rail in sorted(rails, key=int):
        entry = rails[rail]
        rows.append({
            "rail": rail,
            "leaf": ", ".join(sorted(entry["leaves"])),
            "leaf_count": len(entry["leaves"]),
            "nodes": len(entry["nodes"]),
            # PASS/FAIL, matching the power report. An earlier version used
            # "CHECK", which reads as easily as "checked, fine" as it does as
            # "look at this" -- a status that needs interpreting is not a status.
            "verdict": "PASS" if len(entry["leaves"]) == 1 and len(entry["nodes"]) == 32
                       else "FAIL",
        })

    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    return rows


def main():
    try:
        nb, info = connect()
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    os.makedirs(EXPORT_DIR, exist_ok=True)
    print(f"{info['url']}\n")

    power_path = os.path.join(EXPORT_DIR, "power-report.csv")
    rows = power_report(nb, power_path)
    print(f"wrote {power_path}")

    total = rows[-1]
    print(f"  pod load:        {total['allocated_w'] / 1000:.1f} kW")
    print(f"  racks:           {len(rows) - 1}")
    print(f"  N-1 verdict:     {total['n1_ok']}")
    worst = max(
        (r for r in rows[:-1] if r["feeds"]), key=lambda r: r["n1_util_pct"]
    )
    print(f"  worst N-1 rack:  {worst['rack']} at {worst['n1_util_pct']}%")

    cable_path = os.path.join(EXPORT_DIR, "cable-schedule.csv")
    cables = cable_schedule(nb, cable_path)
    print(f"\nwrote {cable_path}")
    print(f"  cables:          {len(cables)}")
    over = [c for c in cables if c["reach_check"].startswith("over")]
    limit = [c for c in cables if c["reach_check"].startswith("OVER")]
    print(f"  over 30m optimum: {len(over)}")
    print(f"  over 50m limit:   {len(limit)}")

    rail_path = os.path.join(EXPORT_DIR, "rail-map.csv")
    rails = rail_map(nb, rail_path)
    print(f"\nwrote {rail_path}")
    for r in rails:
        print(f"  rail {r['rail']} -> {r['leaf']}  ({r['nodes']} nodes)  {r['verdict']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
