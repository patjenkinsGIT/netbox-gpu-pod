# Commissioning Checklist — Reference GPU Pod

One DGX SuperPOD scalable unit: 12 racks, 32× DGX H100, 358 kW, 964 cables.

Derived from the NetBox model in this repository. Quantities and identifiers
below come from `exports/power-report.csv` and `exports/cable-schedule.csv`, so
the checklist and the model cannot drift apart.

> Sequenced so that each stage's failures are cheap. Anything that can be caught
> on the dock should not be caught at power-on, and anything catchable at
> power-on should not be caught during a customer's first training run.

---

## 1. Site readiness — before anything ships

- [ ] **Floor loading confirmed** against 521.8 kg of compute per compute rack
      (4 × 130.45 kg), before rack weight, PDUs and cabling. Eight racks.
- [ ] **Three independent power sources per rack** available and terminated —
      not three breakers on one panel. N+1 means N=2 here, each source sized to
      carry 50% of peak.
- [ ] Circuit rating verified per rack: **415 VAC / 60 A / three-phase**.
      Confirm which derating the facility applies — NetBox computes 34,501 VA
      usable, NVIDIA's table states 32.7 kW for the same circuit. Resolve the
      difference on paper before it matters.
- [ ] **Cooling capacity ≥ 40.8 kW per compute rack**, and airflow containment
      appropriate to front-to-rear equipment.
- [ ] Cable pathways surveyed. **No InfiniBand run may exceed 50 m total
      travel; target under 30 m.** Confirm end-of-row leaf placement is
      reachable from the furthest compute rack — this is the constraint most
      likely to be discovered too late.
- [ ] Rack positions marked to the floor plan; 800 × 1200 mm footprints with
      rear service clearance.

## 2. Receiving and BOM reconciliation

- [ ] Count against the model, not the packing list. Both, then compare:
      **32 DGX H100 · 14 QM9700 · 2 SN4600C · 2 SN2201 · 4 storage nodes ·
      36 rPDUs · 12 racks**
- [ ] Serial numbers recorded against intended device name (`dgx-c03-02`, not
      "DGX #14") at the point of unboxing. Renaming later is where asset
      records go wrong permanently.
- [ ] Optical transceiver and cable counts reconciled against
      `cable-schedule.csv` — 964 cables, by type and length.
- [ ] Visible freight damage photographed before the carrier leaves. Crates
      retained until power-on passes.
- [ ] Any shortfall raised same-day. A missing leaf switch discovered at
      cabling stage stalls an entire rail.

## 3. Rack and stack

- [ ] Racks placed, levelled, bonded to ground, seismic anchoring if required.
- [ ] **Compute racks: DGX at U1, U9, U17, U25.** 8U each, 32U of 48U used.
      The remaining 16U stays empty **by power, not by oversight** — four
      systems already draw 40.8 kW. Note it on the rack so nobody helpfully
      fills the gap.
- [ ] Network racks: leaf at U1–U4, spine at U10–U11.
- [ ] Management rack: SN4600C at U1 and U3 (2U each), SN2201 at U10 and U11.
- [ ] Storage rack: fabric switches U1–U2, storage nodes U5, U7, U9, U11.
- [ ] Three 0U rPDUs per rack, mounted in side channels, A/B/C left to right
      and consistent across every rack.
- [ ] Lift equipment used for DGX placement. 130 kg is a two-person-plus-lift
      item, not a two-person item.

## 4. Power

Do this before any data cabling. A rack that will not power up cleanly is
easier to work on when it is not full of fibre.

- [ ] Each rPDU input landed on its own source: `pdu-<rack>-a` → feed A,
      `-b` → B, `-c` → C. Verify against `cable-schedule.csv`.
- [ ] **Per-source load balance checked**: each DGX takes two supplies per
      source (psu1/2 → A, psu3/4 → B, psu5/6 → C). Six supplies, 4+2
      redundancy — four must be energised for the system to run.
- [ ] Switches spread across sources with a rotating start, so 2-PSU devices
      do not all land on A and B.
- [ ] Power on **one rack at a time**, one source at a time. Record actual
      draw per feed and compare with the modelled 13,600 VA per feed.
- [ ] **Source-loss test, per compute rack**: drop one source, confirm the
      rack stays up and the survivors carry 20.4 kW each (59.1% of 34,501 VA).
      Restore, repeat for each source. This is the test the whole power design
      exists to pass — if it is skipped, the redundancy is theoretical.
- [ ] Measured totals reconciled against `power-report.csv`. Investigate any
      rack more than 5% off model before proceeding.

## 5. Fabric

- [ ] **Rail mapping verified by sampling, not by trusting the labels.** Pick
      three nodes in different racks; confirm each one's `ib-rail3` lands on
      `leaf-04`. Repeat for one other rail. A rail-optimised fabric miscabled
      by one position still links, still passes a port check, and quietly
      loses the locality it exists to provide.
- [ ] Leaf uplinks: ports 33–64 to the four spines, 8 per spine. Every spine
      port occupied — 8 leaves × 8 links = 64.
- [ ] Storage fabric, in-band and out-of-band terminated per schedule.
- [ ] Optical link quality checked at both ends. Record any run over 30 m.
- [ ] Bend radius, slack and tray loading inspected before doors close.

## 6. Labelling and records

- [ ] Every cable labelled both ends with the schedule identifier.
- [ ] Device labels match NetBox names exactly. Physical and record must not
      disagree — where they do, the record is what people will trust six
      months from now, and it will be wrong.
- [ ] Rack elevations printed and posted, showing the intentional 16U gap.
- [ ] Serials, MACs and BMC addresses loaded into NetBox.
- [ ] Photographs: front and rear of each rack, doors open, before handover.

## 7. Handover

- [ ] Power report accepted, including the N−1 result per rack.
- [ ] Cable schedule signed off as-built; **discrepancies corrected in the
      model, not annotated on paper.** A model that disagrees with the floor
      is worse than no model.
- [ ] Spares logged: transceivers, cables, PSUs, at least one leaf switch.
- [ ] Known deviations from the reference architecture written down with
      reasons.
- [ ] Escalation contacts and support entitlements recorded.
- [ ] Model re-exported after as-built corrections, so the artifacts and the
      installation match on day one.

---

## Deviations from NVIDIA's reference architecture

Recorded because an undocumented deviation becomes a mystery later.

| Item | Status |
|---|---|
| Storage node model and count | **Assumed** — NVIDIA defers storage to certified partners |
| 2 storage + 2 in-band NICs per DGX | **Assumed** — only the 8 compute rails are published |
| SN2201 dual PSU | **Assumed** — redundancy not stated in the specification |
| Cable lengths | **Nominal** — pending a real floor plan |
| Interface naming (`ib-rail0`…) | Deliberate: real DGX naming (`ibp24s0`) obscures the rail mapping |
