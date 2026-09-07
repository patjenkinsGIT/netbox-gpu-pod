#!/usr/bin/env python3
"""
Delete the cables between a DC busbar's outlets and the trays it feeds.

WHY THIS EXISTS SEPARATELY. build_pod.py creates and updates and never
deletes -- a deliberate limitation, documented in docs/methodology.md, which
keeps the generator incapable of destroying anything. That property is worth
more than the convenience of an in-place fix, so re-allocating the busbar
lives here instead, in a tool that does one narrow thing and says so.

WHEN IT IS NEEDED. Cabling is idempotent by checking whether a termination
already carries a cable. That makes re-runs safe and makes a changed
ALLOCATION invisible: the cables exist, so the generator leaves them alone
and the old mapping survives a spec change indefinitely. Deleting them is
what lets the new allocation take effect on the next build.

WHAT IT TOUCHES. Only cables with one end on a power outlet belonging to a
device of the named type. Feed-to-shelf and shelf-to-busbar cables are left
alone; nothing outside the busbar's own outlets is considered.

Usage:
    python scripts/clear_busbar_cables.py                    # report only
    python scripts/clear_busbar_cables.py --apply            # delete
    python scripts/clear_busbar_cables.py --type gb200-busbar
"""

import argparse
import sys

import pynetbox
import requests

from netbox_client import ConfigError, connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--type",
        default="gb200-busbar",
        help="device type slug whose outlet cables to clear "
             "(default: gb200-busbar)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it, nothing is written",
    )
    args = parser.parse_args()

    try:
        nb, info = connect()
    except ConfigError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    mode = "APPLY -- cables will be deleted" if args.apply else "REPORT ONLY"
    print(f"{info['url']}  [{mode}]\n")

    busbars = {
        d.id: d for d in nb.dcim.devices.all()
        if d.device_type and d.device_type.slug == args.type
    }
    if not busbars:
        print(f"No devices of type {args.type!r} found -- nothing to do.")
        return 0

    # Collected as a set: a cable has two ends, and if both somehow landed on
    # busbar outlets it must still be deleted once, not twice.
    cable_ids = set()
    per_device = {}
    for outlet in nb.dcim.power_outlets.all():
        if outlet.device.id not in busbars or not outlet.cable:
            continue
        cable_ids.add(outlet.cable.id)
        per_device.setdefault(busbars[outlet.device.id].name, 0)
        per_device[busbars[outlet.device.id].name] += 1

    if not cable_ids:
        print("No busbar outlet cables present -- nothing to delete.")
        return 0

    for name in sorted(per_device):
        print(f"  {name}: {per_device[name]} cables")
    print(f"\n{len(cable_ids)} cables total")

    if not args.apply:
        print("\nReport only. Re-run with --apply to delete, and take a "
              "pg_dump first.")
        return 0

    deleted = 0
    try:
        for cable_id in sorted(cable_ids):
            cable = nb.dcim.cables.get(cable_id)
            if cable is None:
                continue
            cable.delete()
            deleted += 1
    except pynetbox.RequestError as exc:
        # Same recovery posture as build_pod.py: report how far it got, and
        # re-running is always safe because the set is recomputed from
        # current state rather than remembered.
        print(f"\ndeleted: {deleted}")
        print(f"FAIL: NetBox rejected a delete: {exc}", file=sys.stderr)
        return 1

    print(f"\ndeleted: {deleted}")
    print("Re-run build_pod.py to re-cable with the current allocation.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.exceptions.ConnectionError as exc:
        print(f"\nFAIL: could not reach NetBox: {exc}", file=sys.stderr)
        sys.exit(1)
