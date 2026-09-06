# Design Notes

Where this model departs from NVIDIA's published material, and why. The point
of the file is that a reader can tell which numbers are sourced, which are
choices, and which are guesses — without having to reverse-engineer that from
the spec.

---

## What NVIDIA prescribes, and what it doesn't

This distinction drove most of the decisions below. The compute side of the
DGX SuperPOD reference architecture is specific: node count per scalable unit,
rack density, leaf and spine counts, power scheme, cable reach. The
infrastructure side explicitly is not.

> "Figure 3 shows an example management rack configuration with networking
> switches, management servers, storage arrays, and UFM appliances. **Sizes and
> quantities will vary depending upon models used.**"
> — [DGX SuperPOD Architecture][arch]

So the management-rack figure is an illustration, not a specification. Matching
it exactly would encode one example's storage-vendor choice as though it were
the reference. What follows treats the compute side as authoritative and the
infrastructure side as a design space.

### Sourced and matched

| | Value | Source |
|---|---|---|
| Nodes per SU | 32 DGX H100 | [architecture][arch] |
| Rack density | 4 systems/rack, >40 kW | [architecture][arch] |
| Leaf / spine per SU | 8 / 4 (Table 3: 32 / 16 across 4 SU) | [architecture][arch] |
| Compute rails per node | 8 × NDR400 | [components][comp] |
| Power scheme | 415 V, 60 A, 3-phase, ≥3 sources, N+1, 50% sizing | [electrical][elec] |
| Rack profile | servers from U3, 3U airflow gap, horizontal rPDUs at top | [infrastructure][infra] |
| Max vertical rPDUs | 2 per rack | [electrical][elec] |
| Cable reach | ≤50 m optical, ≤30 m preferred | [layouts][lay] |

### Chosen, because NVIDIA doesn't specify

| Decision | Choice | Reasoning |
|---|---|---|
| Infrastructure rack count | 4 (N01, N02, M01, S01) | NVIDIA's example uses 2 combined racks. Splitting by function keeps failure domains and power budgets legible per role, and the rack roles then filter cleanly in reports. Either is defensible; this one is easier to read. |
| Storage fabric switches | 2 | The example figure shows 6, sized for a particular storage vendor. With 4 placeholder storage nodes, 2 is proportionate. |
| In-band switches | 2 × SN4600C | 64 ports each; 36 endpoints need one, two gives redundancy. The example shows 4. |
| Management / head nodes | not modelled | Quantity and model are entirely deployment-specific. Modelling invented servers would add weight without adding information. |
| Storage nodes | 4, placeholder | No reference storage node exists to model. |

---

## The 1:1 fabric leaves no room for management

Worth stating because the model makes it visible.

Each QM9700 leaf has 64 ports: 32 downlinks to nodes, 32 uplinks to spines.
Four spines at 8 links per leaf-spine pair means every spine's 64 ports are
also consumed (8 leaves × 8 links). The fabric is strictly 1:1 non-blocking —
and completely full.

That leaves **nowhere to attach the UFM appliances**. Their InfiniBand ports
are therefore left uncabled in this model, and only their out-of-band
management ethernet is connected.

The alternatives, neither free:

- **Reserve fabric ports for management.** Reduces uplinks, making the fabric
  slightly oversubscribed. This is what real deployments do.
- **Add switch capacity.** More ports, more cost, more power.

Silently cabling UFM into a "free" port would have produced a model that looks
complete while quietly breaking the non-blocking property the rail-optimised
topology exists to provide. Leaving it visible is the more useful answer.

---

## Why leaves are end-of-row, not top-of-rack

Rail-optimised means rail *n* on **every** node lands on leaf *n* — so leaf-04
terminates `ib-rail3` from all 32 nodes across all eight compute racks. A
top-of-rack leaf could only ever reach the four nodes in its own rack, which
defeats the topology entirely.

This is not a preference; it is forced by the design. It also brings the 30 m
InfiniBand guidance into play, since the furthest compute rack must reach the
network rack — see `exports/cable-schedule.csv`, which carries a reach check
column for exactly this reason.

---

## Why the QM9700 is budgeted at 1720 W, not 747 W

NVIDIA publishes both: 747 W typical **with passive cables**, 1720 W maximum
**with active cables**. Passive copper cannot span the distances end-of-row
placement requires, so this pod runs optical and the active figure applies.

The topology decision propagates directly into the power budget. Using the
typical figure would understate the network racks by roughly 14 kW.

---

## Why three power sources, not an A/B pair

Four DGX H100 at peak draw **40.8 kW**. No single circuit option in NVIDIA's
electrical table exceeds **32.8 kW**. Two sources cannot carry the rack on loss
of one; three at 50% sizing can.

Modelled as three separate power panels rather than one panel with three
breakers, because the guide's stronger option is three discrete UPS systems on
independent distribution paths. One panel would look redundant while sharing a
single point of failure.

**Validation:** on loss of one source each survivor carries **20.4 kW** —
matching NVIDIA's published peak-server-demand-per-circuit figure, derived
independently from the model rather than copied. See `exports/power-report.csv`.

---

## Why every rack unit is accounted for

The compute rack layout follows the published profile: DGX at U3, U11, U22,
U30; a 3U airflow gap at U19–U21; horizontal rPDUs at the top.

Nothing in it is spare capacity. U1–U2 is below server-lift reach, U19–U21 is
airflow management, U38–U41 is cable management, U42–U47 is the rPDU zone.
There is no 8U contiguous slot for a fifth system — **the layout already
encodes the power limit**, because four systems draw 40.8 kW against circuits
near 32.8 kW. The density was decided upstream of the elevation.

---

## Known discrepancy: feed capacity

NetBox computes the 415 V / 60 A three-phase feed at **34,501 VA**
(415 × 60 × 1.732 × 0.80). NVIDIA's electrical table lists the same circuit at
**32.7 kW**, implying a different derating assumption.

Both figures are reported. The difference is about 5%, which does not change
the N−1 verdict — every rack passes against either number — but it is the kind
of gap that should be resolved against the facility's own standard before
anything is ordered.

---

## Assumptions, restated plainly

Nothing below is sourced:

- Storage node model, count and specifications — no reference node exists
- 2 storage + 2 in-band NICs per DGX (the 8 compute rails **are** sourced)
- SN2201 dual PSU — redundancy not stated on the specification page
- UFM appliance power draw — not published on the pages consulted
- rPDU height (2U) — mounting position and orientation are sourced, height is not
- Cable lengths — nominal, pending a floor plan
- Interface naming (`ib-rail0`…) deliberately deviates from real DGX naming
  (`ibp24s0` style) so the rail mapping is self-documenting

[arch]: https://docs.nvidia.com/dgx-superpod/reference-architecture-scalable-infrastructure-h100/latest/dgx-superpod-architecture.html
[comp]: https://docs.nvidia.com/dgx-superpod/reference-architecture-scalable-infrastructure-h100/latest/dgx-superpod-components.html
[elec]: https://docs.nvidia.com/dgx-superpod/design-guides/dgx-superpod-data-center-design-h100/latest/electrical.html
[infra]: https://docs.nvidia.com/dgx-superpod/design-guides/dgx-superpod-data-center-design-h100/latest/infrastructure.html
[lay]: https://docs.nvidia.com/dgx-superpod/design-guides/dgx-superpod-data-center-design-h100/latest/layouts.html
