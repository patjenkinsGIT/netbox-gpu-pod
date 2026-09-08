# Design Notes

Where this model departs from NVIDIA's published material, and why. The point
of the file is that a reader can tell which numbers are sourced, which are
choices, and which are guesses — without having to reverse-engineer that from
the spec.

Two pods are modelled, in two NetBox sites:

| Site | Design | Spec |
|---|---|---|
| Reference Pod 1 | 32× DGX H100, air-cooled, 12 racks | `spec/pod.yaml` |
| Reference Pod 2 | 8× GB200 NVL72, liquid-cooled, 9 racks | `spec/pod-gb200.yaml` |

Everything down to **Assumptions, restated plainly** concerns Pod 1. The
liquid-cooled pod starts at **Reference Pod 2 — the liquid-cooled pod**, and
carries a much longer assumption list, for a reason given there.

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
network rack — see `exports/h100-cable-schedule.csv`, which carries a reach
check column for exactly this reason.

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
independently from the model rather than copied. See
`exports/h100-power-report.csv`.

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

---

# Reference Pod 2 — the liquid-cooled pod

8× GB200 NVL72 plus a CDU rack, in site **Reference Pod 2**. 314 devices,
344 power cables, a three-tier cooling topology, **977.2 kW**.

## Why a separate site rather than racks in Data Hall A

Three reasons, in order of weight:

1. Pod 1 is a finished, published artifact. Mixing GB200 racks into it
   invalidates its screenshots, its rack counts and its exports.
2. `CoolingSource` is scoped to a site, so a new site gives the cooling plant
   a clean home instead of threading it past Panel A/B/C.
3. The comparison this phase exists to produce is a per-site filter, which
   keeps the export script simple.

A fourth reason only appeared afterwards, and is the better argument. The
export script iterated every rack in NetBox — correct while one pod existed,
silently wrong the moment a second did. A second **site** exposed that. A
second row of racks in the same site would have hidden it, because the
merged total would have looked like a bigger version of a right answer.

## What NVIDIA publishes for GB200, and what it does not

Pod 1 could lean on a data-center design guide: electrical tables, rack
layouts, cable-reach guidance, a published rack profile down to the airflow
gap. **There is no GB200 equivalent.** The `design-guides/` set stops at
H100. Every electrical and thermal figure for Pod 2 comes from the reference
architecture and the user guide, which describe the *system* rather than the
*facility*.

That absence is why the assumption list below is far longer than Pod 1's, and
why the rack elevation here is invented rather than reproduced.

### Sourced and matched

| | Value | Source |
|---|---|---|
| Racks per SU | 8 NVL72 rack systems | [GB200 architecture][gbarch] |
| SU thermal design power | 1.2 MW | [GB200 architecture][gbarch] |
| Rack contents | 18× 1RU compute tray, 9× 1RU NVLink switch tray, 2× ToR | [user guide][gbhw] |
| Per compute tray | 2 Grace CPU, 4 Blackwell GPU | [user guide][gbhw] |
| Rack power | ≈120 kW | [user guide][gbhw] |
| Power shelves | 8 shelves, 6× 5.5 kW PSU each, 33 kW output, N+N | [GB200 components][gbcomp] |
| Distribution | AC whips from a remote panel → 50–51 V DC busbar | [user guide][gbhw] |
| Cooling | hybrid — GPUs and CPUs liquid, everything else air | [GB200 architecture][gbarch] |
| Compute fabric NICs | 4× ConnectX-7 400G OSFP per tray | [user guide][gbhw] |
| Storage / in-band NICs | 2× BlueField-3, dual-port 400G | [user guide][gbhw] |
| Storage fabric | Spectrum SN5600, 800 GbE — **not** InfiniBand | [GB200 components][gbcomp] |

**Four rails per tray, not eight.** The rail count follows GPUs per chassis,
so a 4-GPU tray has four. That is the largest single fabric difference from
Pod 1 and it changes the shape of the whole compute fabric.

### Rejected: a figure that is widely repeated and is not NVIDIA's

Several secondary write-ups state that NVIDIA specifies **three independent
power sources per rack** for GB200. It does not. That is the H100 electrical
rule being carried across by people summarising, and NVIDIA's GB200 material
says N+N on the power shelves instead.

Treating it as sourced would have repeated the S7 mistake — taking something
that looks authoritative for something that is — in the opposite direction.
Recorded here because a reader searching the same terms will find the claim.

## Why the power chain grew a hop, and lost its arithmetic

Pod 1: `PowerFeed → rPDU → outlet → device PSU`. Three hops, every one a
one-to-one cable.

Pod 2: `PowerFeed → power shelf → busbar → tray`. Four, because AC-to-DC
conversion moved out of the servers and into the rack.

The busbar is where NetBox's power model stops fitting. **NetBox represents
power as a tree of one-to-one cables. A busbar is a single shared plane** —
eight shelves push into it, twenty-seven trays pull from it, and there is no
correspondence between any particular shelf and any particular tray.

It is modelled here as a device with 8 input ports and 32 outlets divided four
per input. **That division is fiction**, chosen so the totals aggregate
correctly to the feeds. It says nothing true about which shelf carries which
tray, because on a shared bus the question has no answer.

### The fiction is not neutral — it produces numbers

The first allocator dealt trays round-robin. The tray list sorts by name, so
eighteen 5,400 W compute trays took the first eighteen slots and nine 2,500 W
NVLink trays trailed behind; shelves 1 and 2 collected three heavy trays each
and read **18,700 VA** while shelves 4–8 read 13,300. A 40% spread invented
entirely by alphabetical order, and it reported a worst feed of **54.2%**
against a true shared-bus figure of **43.4%** — a hotspot with no physical
existence, on its way into the power report.

Reallocating heaviest-first onto the least-loaded shelf narrows it to
13,300–16,200 VA, or 47.0% against 43.4%. It cannot close it: 119,700 W does
not divide evenly into eight discrete trays.

**So the per-feed figure is a property of our allocator and the rack total is
a property of the design.** The exports must distinguish them rather than
present both as measurements.

## Why N+N replaces N+1

Pod 1 uses three sources at 50% sizing because NVIDIA's H100 electrical guide
says so, and because four DGX H100 at 40.8 kW exceed any single listed
circuit. Pod 2 uses two panels because NVIDIA states the eight power shelves
are **N+N** — two sides, each able to carry the whole load — and eight feeds
alternate between them.

The circuit is deliberately unchanged: 415 V, 60 A, three-phase, 80% derate,
34,501 VA. Two consequences fall out.

**Feeds per rack go from 3 to 8.** The same circuit that fed a third of a DGX
H100 rack now feeds one power shelf.

**And it is marginal.** A shelf's 33 kW *output* against 34,501 VA of derated
capacity is roughly 96% loaded before conversion losses are counted at all. A
real design would step up to a larger circuit. Modelled as-is so the report
shows why rather than burying it in a choice.

### The N+N verdict is asserted, not derived

Pod 1's headline validation was that the N−1 case produced **20.4 kW per
surviving feed** — NVIDIA's own published figure, reached by NetBox walking
the cable path rather than by copying it.

Nothing equivalent is available here. Losing one panel means the load shifts
onto the surviving half of a shared bus, and no cable in the model represents
that. The figure — **86.7% of feed capacity**, against Pod 1's 59.1% — is
arithmetic performed on the model, not walked out of it. That difference is
worth more than the number.

## What NetBox 4.7's cooling model can and cannot express

4.7 shipped a real cooling model, and it mirrors the power model closely
enough to reuse the same techniques — until it doesn't.

| | Power | Cooling |
|---|---|---|
| Plant object | PowerPanel — name only | CoolingSource — capacity, fluid, type, status |
| Feed | capacity **and** derate | capacity, **no derate** |
| Feed → device | Cable | **nothing** |
| Component demand | `allocated_draw` / `maximum_draw` | **nothing** |
| Downstream parenting | `power_port` FK | `cooling_intake` FK — transfers exactly |

The last row is real and useful: a CDU and a rack manifold are built exactly
like an rPDU — one intake, many outflows, each outflow naming its parent — and
the S3 trap transfers with it. An outflow with no parent intake leaves no
path, silently.

The two rows above it are the limitation. **The cooling model carries capacity
at every level and demand at none**, and there is no cable or foreign key
joining a feed to a device component. So it will hold a complete, correct
topology and compute nothing from it.

Two things follow, and both are visible in `exports/gb200-cooling-report.csv`:

**Every cooling load figure is derived by us from the power model**, and the
report carries a `basis` column saying so on every row. 815.2 kW of liquid
load against 1,200 kW of installed plant is our arithmetic, resting on an
assumed 85% liquid fraction. It has none of the standing of the 20.4 kW
figure in Pod 1.

**Two intakes can never be wired.** The CDUs' `fws-in` intakes take facility
water from a `CoolingFeed`, and an intake has no foreign key to a feed. The
report separates `unwired_at_plant` from `unwired_unexplained` for exactly
this reason — a count that is permanently non-zero and unexplained is one
readers learn to skip.

## Rack cooling capacity belongs to the cabinet, not the rack

`cooling_capability` and `cooling_capacity` are inherited from the rack
**type**. A per-rack value is accepted by the API, returns 200, and is
discarded — the same way `u_height` and `width` are.

This cost a real bug. X01 declared `air-only` and 20 kW; the generator
reported `updated: 1`; the next dry run reported the same update still
pending. `validate_spec()` now rejects rack-level cooling overrides outright.

It also has a design consequence worth stating: **two racks of the same
cabinet model cannot differ in cooling capacity**, even though cooling
capacity depends on what is installed and how the room is arranged rather
than on the cabinet. The CDU rack's ~18 kW of pump and electrical load has
nowhere in the model to live.

### Why no rack declares a cooling capacity

Both rack types declare `cooling_capability` and neither declares
`cooling_capacity`, so every rack in both pods reports **NO CAPACITY SET**.

That is deliberate. An earlier version set the NVL72 type to 120 kW, reusing
NVIDIA's published rack **power** figure. Those are different quantities —
one is how much heat the rack makes, the other how much it can reject — and
setting them equal made the report compare a number against itself and print
`119.9 of 120.0 PASS`. A verdict with 0.1 kW of margin, which was arithmetic
rather than thermal analysis.

NVIDIA publishes no cooling capacity for either cabinet. A column of blanks
that says so is worth more than a PASS that was never earned.

## The two pods compared

From `exports/pod-comparison.csv`. **Compute racks only** — Pod 2 has no
network or management racks modelled yet, so a whole-pod rack count would
flatter it by comparing eight racks against twelve.

| | Pod 1 (H100) | Pod 2 (GB200) | |
|---|---|---|---|
| GPUs | 256 | **576** | 2.25× |
| Compute racks | 8 | 8 | 1.00× |
| GPUs per compute rack | 32 | **72** | 2.25× |
| kW per compute rack | 40.8 | **119.9** | 2.94× |
| kW per GPU | 1.275 | 1.665 | 1.31× |
| Compute racks per 1,000 GPUs | 31.2 | **13.9** | 0.45× |
| kW per 1,000 GPUs | 1,275 | **1,665** | 1.31× |
| Feeds per compute rack | 3 | 8 | 2.67× |
| Redundancy | N+1, three sources at 50% | N+N, two sides at 100% | |
| Worst surviving feed | 59.1% | **86.7%** | 1.47× |
| Cooling | air | hybrid liquid + air | |

**The headline is the trade, not the density.** Same GPU count needs 56%
fewer racks and 31% more power to run them. The energy is not saved, it is
concentrated — and the electrical margin after the worst credible loss
narrows from 59% to 87% while that happens.

## Assumptions, restated plainly — Reference Pod 2

Longer than Pod 1's list, because there is no GB200 design guide. Nothing
below is sourced:

- **The rack elevation is entirely invented.** NVIDIA lists what an NVL72
  contains and never says where any of it sits. Compare Pod 1, whose rack
  profile is sourced down to the 3 RU airflow gap.

  It also leaves **U36–U46 empty**, which is visible in the elevation and is
  worth naming before someone asks why a 120 kW rack is a quarter vacant. The
  37 U of equipment is sourced — 18 compute trays, 9 NVLink trays, 8 power
  shelves, 2 ToR — and the coolant manifold is modelled as 0U in the side
  channel. So the free space is real, but its *position* is an artefact of
  stacking everything from the bottom. A published layout would distribute it,
  the way Pod 1's profile puts space below server-lift reach and between
  server groups for airflow. Nothing here is sourced well enough to do that,
  and guessing at a distribution would look more authoritative than it is.

  One correction did come out of looking at the elevation, though. The ToR
  switches were first placed at U36–U37, with eleven empty units above them —
  a top-of-rack switch that was not at the top of the rack. The position is
  still a choice rather than a sourced fact, but a name and a position that
  contradict each other are a defect in any layout, and in an invented one
  there is nothing to appeal to except internal coherence.
- **Rack dimensions, height in U and weight** — 48U, 600 × 1068 × 2236 mm,
  1.36 t come from OEM product listings, not from NVIDIA. Whether the rack is
  19-inch or 21-inch ORV3 is not settled by anything NVIDIA publishes; 19-inch
  is modelled because NVIDIA's own text says "1RU trays".
- **Per-tray power.** NVIDIA publishes the ≈120 kW rack total and no
  breakdown. 5,400 W per compute tray is two GB200 superchips at a
  widely-reported 2,700 W each — a figure from secondary sources, not from an
  NVIDIA page consulted here. 2,500 W per NVLink tray is the residual that
  closes the sourced rack total.
- **Whether NVLink switch trays are liquid-cooled.** NVIDIA says GPUs and CPUs
  are; NVSwitch is not named either way.
- **The CDU entirely** — model, count, capacity, connector, facility water
  condition. No CDU is named in NVIDIA's GB200 material. Placeholder, in the
  same spirit as Pod 1's storage node.
- **The rack manifold entirely** — 27 drops, UQD connectors, line sizes.
- **The 85% liquid fraction**, which every derived cooling number rests on.
- **ΔT of 10 K**, from which every flow rate is computed.
- **Two panels rather than three**, matching the stated N+N; the number of
  upstream sources is not specified.

### Named gaps, not oversights

- **No network or management racks.** Compute fabric switch counts per SU and
  SN5600 specifications are not yet sourced. Adding invented infrastructure
  racks to improve a racks-per-1,000-GPUs headline is exactly the failure this
  project keeps writing up.
- **ToR switch power is uncabled.** NVIDIA says the rack contains two ToR
  switches and does not say what powers them; there is no AC distribution
  inside an NVL72. Their 196 W per rack appears in the power report's
  `uncabled_w` column rather than being quietly attached to something.
- **CDU power is uncabled**, for the same reason — 18 kW, also in that column.
- **No fabric cabling.** The compute fabric is 4 rails on Spectrum-based
  storage rather than Pod 1's 8 rails on InfiniBand, and modelling it needs
  the switch counts above.

Together those account for the **19,568 W** the power report flags as drawn
with no feed path — a documented gap turned into a number that is re-verified
on every run rather than a claim in a comment.

[gbarch]: https://docs.nvidia.com/dgx-superpod/reference-architecture-scalable-infrastructure-gb200/latest/dgx-superpod-architecture.html
[gbcomp]: https://docs.nvidia.com/dgx-superpod/reference-architecture-scalable-infrastructure-gb200/latest/dgx-superpod-components.html
[gbhw]: https://docs.nvidia.com/dgx/dgxgb200-user-guide/hardware.html
[arch]: https://docs.nvidia.com/dgx-superpod/reference-architecture-scalable-infrastructure-h100/latest/dgx-superpod-architecture.html
[comp]: https://docs.nvidia.com/dgx-superpod/reference-architecture-scalable-infrastructure-h100/latest/dgx-superpod-components.html
[elec]: https://docs.nvidia.com/dgx-superpod/design-guides/dgx-superpod-data-center-design-h100/latest/electrical.html
[infra]: https://docs.nvidia.com/dgx-superpod/design-guides/dgx-superpod-data-center-design-h100/latest/infrastructure.html
[lay]: https://docs.nvidia.com/dgx-superpod/design-guides/dgx-superpod-data-center-design-h100/latest/layouts.html
