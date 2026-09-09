# Roadmap

What is done, what is deliberately left, and what was considered and rejected.
The rejections are the useful part of this file: a model like this can always be
made bigger, and most of the ways of doing that add weight without adding
information.

The original build plan ran S0 through S8 and is complete — S8 being the
liquid-cooled GB200 pod. Everything below was decided after that, on
**2026-09-09**.

## Done since the plan ended

- **Continuous integration.** `.github/workflows/build.yml` stands up NetBox in
  a container, runs `build_pod.py` against both specs from an empty database,
  runs the exports, and asserts the published figures — 359.6 kW, 977.2 kW, 970
  and 344 cables, N−1 PASS, the 19,568 W uncabled gap. Before this, a reader had
  to take the committed CSVs on trust. The badge in the README is the claim that
  the numbers on that page are reproduced rather than remembered.

  It also retroactively covers a real bug: an earlier dry run against an empty
  NetBox failed on `KeyError: 'rack_type'`, which CI would have caught on the
  push that introduced it.

## Open — roughly in order of how much they'd add

### 1. Prune mode — close the deletion gap

`docs/methodology.md` ends on *"convergence is guaranteed in one direction
only."* The generator creates and reconciles; it never deletes. Removing a rack
from a spec leaves it in NetBox, and the run reports success.

Closing that means a prune mode with a dry run that names exactly what would be
destroyed and a confirmation gate that cannot be defaulted through. It is the
hardest engineering left and it retires a limitation the docs currently just
admit to. `scripts/clear_busbar_cables.py` exists because of this gap — a narrow,
hand-written deletion tool for one case that came up.

### 2. A floor plan — retire the biggest phase-1 assumption

Cable lengths are nominal. They exist so the 30 m and 50 m reach checks in the
cable schedules have something to check, and `docs/design-notes.md` says so.

Real rack coordinates would make `exports/h100-cable-schedule.csv` and
`exports/gb200-cable-schedule.csv` genuinely useful rather than illustrative, and
would put actual pressure on the end-of-row leaf placement that the
rail-optimised topology forces. Bounded work with a clear finish.

### 3. The named gaps in Pod 2

GB200 network and management racks, compute fabric cabling, CDU and ToR power.
These are listed as **Named gaps, not oversights** in `docs/design-notes.md` and
together account for the 19,568 W the power report flags as drawn with no feed
path.

Closing them would let the comparison table quote whole-pod
racks-per-1,000-GPUs instead of compute-racks-only. It is also the lowest new
signal of anything here — the same modelling already done twice — and it needs
switch counts per SU that are not yet sourced. Inventing them to improve a
headline is precisely the failure this project keeps writing up.

### 4. Write up the methodology

Not code. `docs/methodology.md` documents six things that reported success while
being wrong — a dry run that passed against an empty database, an update that
applied cleanly and changed nothing, a power allocator that invented a hotspot
out of alphabetical order. That is a better essay than it is an appendix, and it
travels further than a repository does.

## Considered and rejected

**A third pod — GB300, or an air-cooled B200.** It would produce a third set of
the same artifacts: another spec, another elevation, another power report, another
column. The comparison in `docs/design-notes.md` earns its keep because air-cooled
H100 and liquid-cooled GB200 differ in kind — cooling, redundancy scheme, power
chain topology, rails per node. A third pod that differs only in degree makes the
table wider without making it say more.

**Rack-level cooling capacity.** Rejected on evidence, not taste, and written up
in `docs/design-notes.md`: NetBox inherits `cooling_capacity` from the rack type
and silently discards per-rack overrides, and the one time a capacity was set it
came from NVIDIA's rack *power* figure, which made the report compare a number
against itself and print `119.9 of 120.0 PASS`. `validate_spec()` now rejects
rack-level cooling overrides outright.

**Cabling UFM into the compute fabric.** The 1:1 non-blocking topology consumes
every port on every leaf and spine. Attaching UFM to a "free" port would have
produced a model that looks complete while quietly breaking the property the
rail-optimised design exists to provide. The InfiniBand ports are left uncabled
and the reasoning is in `docs/design-notes.md`.

## Finishing is also an option

Worth stating explicitly, because a repository with a roadmap invites the
assumption that the roadmap should be worked through. Both pods converge, CI
proves it on every push, and the documentation distinguishes sourced figures from
assumed ones throughout. Each item above has real diminishing returns against
simply leaving it here.
