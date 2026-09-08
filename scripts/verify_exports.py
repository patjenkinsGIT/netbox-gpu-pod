#!/usr/bin/env python3
"""
Assert the headline figures in the exports, and say which ones failed.

WHY THIS EXISTS. Every number in the README and the design notes came out of a
CSV that a person read once. That is exactly the arrangement this project has
been writing up as untrustworthy for two phases: a result nobody re-checks is a
result that decays silently the first time something upstream changes.

This turns each published figure into an assertion. It reads only the exports,
not NetBox, so it can run anywhere the CSVs are -- locally after a build, or in
CI against a pod rebuilt from empty.

The figures below are the ones quoted in README.md and docs/design-notes.md. If
a number changes for a good reason, this file is where the change gets
acknowledged rather than where it gets discovered.

Usage:
    python scripts/verify_exports.py
    python scripts/verify_exports.py --dir exports
"""

import argparse
import csv
import os
import sys

from netbox_client import REPO_ROOT


class Checker:
    """Collects PASS/FAIL rows under section headings, then prints them.

    Sections are recorded rather than printed as they are reached. An earlier
    version printed each heading immediately and buffered the rows, so all
    three headings appeared first and thirty-three rows arrived underneath as
    one block -- with "racks 12" and "racks 9" adjacent and nothing saying
    which pod either belonged to. A label that does not sit with its data is
    not a label.
    """

    def __init__(self):
        self.entries = []

    def section(self, title):
        self.entries.append(("section", title))

    def check(self, label, actual, expected):
        ok = actual == expected
        self.entries.append(("check", (label, actual, expected, ok)))
        return ok

    def report(self):
        checks = [e[1] for e in self.entries if e[0] == "check"]
        width = max(len(c[0]) for c in checks)
        failed = 0
        for kind, payload in self.entries:
            if kind == "section":
                print(f"\n{payload}\n")
                continue
            label, actual, expected, ok = payload
            verdict = "PASS" if ok else "FAIL"
            if ok:
                print(f"  {verdict}  {label:<{width}}  {actual}")
            else:
                failed += 1
                print(f"  {verdict}  {label:<{width}}  {actual}  "
                      f"(expected {expected})")
        print()
        if failed:
            print(f"{failed} of {len(checks)} checks FAILED")
        else:
            print(f"all {len(checks)} checks PASS")
        return failed


def load(directory, name):
    path = os.path.join(directory, name)
    if not os.path.exists(path):
        raise SystemExit(f"FAIL: missing export {path}")
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def total_row(rows):
    """The SITE TOTAL row, which every report ends with."""
    for row in rows:
        if row.get("rack") == "SITE TOTAL":
            return row
    raise SystemExit("FAIL: no SITE TOTAL row -- report format changed")


def rack_rows(rows):
    return [r for r in rows if r.get("rack") != "SITE TOTAL"]


def kw(row, field="allocated_w"):
    return round(int(row[field]) / 1000.0, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default=os.path.join(REPO_ROOT, "exports"),
        help="directory holding the exports (default: exports/)",
    )
    args = parser.parse_args()
    c = Checker()

    # ---------------------------------------------------------- pod 1, H100
    c.section("Reference Pod 1 — air-cooled DGX H100")
    power = load(args.dir, "h100-power-report.csv")
    racks = rack_rows(power)
    total = total_row(power)

    c.check("racks", len(racks), 12)
    c.check("devices", int(total["devices"]), 92)
    c.check("site load kW", kw(total), 359.6)
    # Zero is the whole point: every supply in this pod has a path to a feed.
    c.check("uncabled W", int(total["uncabled_w"]), 0)
    c.check("redundancy scheme", total["redundancy"], "N+1")
    c.check("racks passing N-1", sum(r["verdict"] == "PASS" for r in racks), 12)
    # The figure the project is built on: on loss of one of three sources each
    # survivor carries 20.4 kW, which is NVIDIA's own published number reached
    # by walking the model rather than by copying it.
    worst = max(racks, key=lambda r: float(r["loss_util_pct"]))
    c.check("worst N-1 utilisation %", float(worst["loss_util_pct"]), 59.1)
    c.check("worst N-1 per-feed W", int(worst["loss_per_feed_w"]), 20400)

    cables = load(args.dir, "h100-cable-schedule.csv")
    c.check("cables", len(cables), 970)
    c.check("cables over 50 m limit",
            sum(r["reach_check"].startswith("OVER") for r in cables), 0)

    rails = load(args.dir, "h100-rail-map.csv")
    c.check("rails", len(rails), 8)
    c.check("rails passing", sum(r["verdict"] == "PASS" for r in rails), 8)
    c.check("one leaf per rail",
            sum(int(r["leaf_count"]) == 1 for r in rails), 8)
    c.check("32 nodes per rail", sum(int(r["nodes"]) == 32 for r in rails), 8)

    # --------------------------------------------------------- pod 2, GB200
    c.section("Reference Pod 2 — liquid-cooled GB200 NVL72")
    power2 = load(args.dir, "gb200-power-report.csv")
    racks2 = rack_rows(power2)
    total2 = total_row(power2)

    c.check("racks", len(racks2), 9)
    c.check("devices", int(total2["devices"]), 314)
    c.check("site load kW", kw(total2), 977.2)
    c.check("redundancy scheme", total2["redundancy"], "N+N")
    # NOT zero, deliberately: the ToR switches and the CDUs draw power that no
    # modelled circuit provides, because NVIDIA does not say what feeds them.
    # A documented gap, held as a number so it cannot drift unnoticed.
    c.check("uncabled W (ToR + CDU)", int(total2["uncabled_w"]), 19568)

    nvl72 = [r for r in racks2 if r["role"] == "Compute"]
    c.check("NVL72 racks", len(nvl72), 8)
    c.check("per-rack load kW", kw(nvl72[0]), 119.9)
    # The busbar allocator's residual spread. Not a design property -- an
    # artefact of 27 discrete trays not dividing evenly into 8 shelves. Pinned
    # so that a change to the allocator has to be acknowledged here.
    c.check("worst shelf feed VA", int(nvl72[0]["worst_feed_w"]), 16200)
    c.check("loss-of-side utilisation %", float(nvl72[0]["loss_util_pct"]), 86.7)

    cables2 = load(args.dir, "gb200-cable-schedule.csv")
    c.check("cables", len(cables2), 344)

    cooling = load(args.dir, "gb200-cooling-report.csv")
    ctotal = total_row(cooling)
    c.check("cooling intakes", int(ctotal["intakes"]), 226)
    c.check("intakes wired", int(ctotal["intakes_wired"]), 224)
    # Two, forever: the CDUs' facility-water intakes, which NetBox has no way
    # to join to a cooling feed. Expected and explained.
    c.check("unwired at plant boundary", int(ctotal["unwired_at_plant"]), 2)
    # This one must always be zero. Anything else is a real plumbing gap.
    c.check("unwired unexplained", int(ctotal["unwired_unexplained"]), 0)
    c.check("derived liquid load kW", float(ctotal["liquid_load_kw"]), 815.2)

    # ------------------------------------------------------- the comparison
    c.section("Comparison")
    comp = {r["metric"]: r for r in load(args.dir, "pod-comparison.csv")}
    c.check("H100 GPUs", int(comp["GPUs"]["h100"]), 256)
    c.check("GB200 GPUs", int(comp["GPUs"]["gb200"]), 576)
    c.check("racks per 1,000 GPUs, H100",
            float(comp["Compute racks per 1,000 GPUs"]["h100"]), 31.2)
    c.check("racks per 1,000 GPUs, GB200",
            float(comp["Compute racks per 1,000 GPUs"]["gb200"]), 13.9)

    return 1 if c.report() else 0


if __name__ == "__main__":
    sys.exit(main())
