# From Create to Converge

How this generator was built, in eight stages, and what each stage cost to learn.

A generator that populates a database and a generator that reconciles one look
identical right up until the moment they disagree. This is the record of building
the second kind.

| | |
|---|---|
| Racks modelled | 12 |
| Devices | 92 |
| Cables | 970 |
| Pod load | 359.6 kW |
| Failures that reported success | 4 |

---

## How the work was run

**One pair of hands.** Every command was run by the operator. Code and values were
supplied, never executed against the environment — including diagnostics.

**Preview, then apply.** Every change was dry-run first. Two bugs surfaced during
previews that had written nothing.

**Source, don't recall.** Every specification figure was fetched from NVIDIA's
published documentation and cited in `spec/pod.yaml`. Nothing quoted from memory.

**Test the restore.** A database dump was taken before each bulk write — and the
restore procedure was exercised once, while losing everything would have cost nothing.

**Label the guesses.** Every figure is marked sourced or assumed in the spec itself,
so the distinction survives into the artifact instead of living in someone's head.
See [design-notes.md](design-notes.md).

**Check against the world.** The model was compared to the vendor's own drawings.
That comparison found a modelling error — and then found that the drawing was not a
specification.

---

## Eight stages

### 01 — Build one of everything by hand

One rack, one device type, one complete power chain, clicked through the UI before
any code existed.

This is not throat-clearing. It establishes what correct looks like, and it is the
only reference the generator can later be checked against.

It also surfaced the object model's defining asymmetry: **NetBox stamps components
onto a device from its type at creation time and never revisits them.** Fixing a
template leaves every already-built device wrong. That caused three separate bugs
over the course of the build.

### 02 — Get-or-create, with the lookup separated from the payload

The core helper takes both a *lookup* — how to find an existing object — and a
*payload* — what to send when creating one. They are deliberately different shapes,
because NetBox filters on `site_id` and writes with `site`.

Conflating them is the classic cause of a get-or-create that silently duplicates:
the lookup fails to match what already exists, so it creates another one.

```
$ python scripts/build_pod.py    →    existing: 21, created: 0
```

### 03 — A dry run you can trust the numbers from

The first preview reported four racks as *skipped* that an apply would have created
without issue — their role simply did not exist yet, because previews write nothing.
The output conflated "genuinely blocked" with "I haven't made its parent."

A `Planned` sentinel threaded through each object's declared dependencies fixed it,
so previewed counts match applied counts.

At twelve racks you can reason around a wrong preview. At 692 cables you cannot, and
a preview you have to mentally correct is one you stop reading.

### 04 — Declarations instead of instructions

Ninety devices come from eight group declarations with computed rack positions, not
ninety hand-written entries — ninety chances to mistype a rack unit reduced to one
arithmetic expression.

Two mechanical discoveries:

- The REST API does **not** expand `[1-64]` name ranges. That is a UI-form
  convenience only; `expand()` bridges it.
- Component templates must be created in one bulk request, or a 64-port switch
  becomes 64 round trips with a partial-failure mode.

### 05 — Idempotency when there is no name to match on

A rack has a unique name. A cable is just a relationship between two endpoints.
Idempotency there means asking whether each termination already carries a cable, and
being deterministic about which free port is taken next.

The two fabrics needed opposite strategies:

- **Power** — any outlet on the correct PDU is equivalent, so lowest-numbered-free
  allocation is fine.
- **Compute fabric** — *which* port a cable lands on **is** the design. Rail 3 on
  node 1 and rail 3 on node 32 must reach the same leaf. A nearest-free allocator
  would produce a fully cabled fabric with none of the locality the topology exists
  to provide, and nothing in NetBox would flag it.

### 06 — Existence is not convergence

The turning point.

One rack's power feed read 13,600 VA; another read **0 VA** behind a perfectly
healthy "Reachable" cable. A device-type template still carried a stale
`maximum_draw` — the value that suppresses NetBox's downstream aggregation — and all
33 generated rPDUs had inherited it.

**The generator reported `created: 0` and total convergence while a third of the pod
was wrong**, because it checked only whether objects *existed*, never whether their
fields matched the spec.

Three changes closed it:

1. The spec declares `maximum_draw: null` **explicitly**. An omitted field means
   "don't care" and can never be detected as drift; an explicit null is checkable
   intent.
2. `ensure_components()` diffs declared attributes and reports them as `~` updates.
3. `sync_device_power_ports()` pushes device-type values down onto devices already
   created from that type, addressing the stage-01 asymmetry directly.

```
~ pdu-c05-a/input: maximum_draw=None  (would update)    ×33
```

### 07 — Valid end states reached by invalid paths

Correcting the rack layout meant moving 32 systems from U1 to U3. But `position` is
the **bottom** unit of an 8U device, so the first move wanted U3–U10 while its
neighbour still occupied U9–U16. NetBox rejects the overlap.

The final arrangement is entirely legal. No sequence of one-at-a-time moves reaches
it.

Moving top-down would have worked — but only because this particular shift went
upward. The general fix is to vacate every device that moves, then place them all.
Correct regardless of direction.

The physical version is identical: you pull all four systems out of the rack, then
re-rack them at the new heights.

### 08 — Verification as a first-class output

The exports do not just report values, they report verdicts: **PASS/FAIL** per rack
for the N−1 power test, per rail for the fabric map, per cable against the optical
reach limit.

A column that states *what correct means* is what makes wrong output obvious rather
than merely present. The rail-map verdict caught a bug in its own report on the first
run — without it, plausible-looking garbage would have been written to a CSV and
shipped.

```
rail 3 -> leaf-04  (32 nodes)  PASS
N-1 verdict: PASS    worst N-1 rack: C01 at 59.1%
```

---

## Four things that reported success while being wrong

| What it said | What was true | Root cause | Caught by |
|---|---|---|---|
| `Path status: Reachable` | Feed drawing 0 VA from a fully cabled rack | A declared `maximum_draw` on a pass-through port *replaces* the computed value instead of informing it — supplying more data broke the calculation | Comparing a rack built by hand against one built by the script |
| `created: 0, updated: 0` | 33 of 36 rPDUs misconfigured | Get-or-create verifies existence and never diffs attributes | Opening a rack the generator built and reading the number |
| `rail 0 -> leaf-01, dgx-c01-01, leaf-02 …` | Plausible-looking nonsense | Interfaces and power outlets are separate tables with independent id sequences; the lookup had no `object_type` filter | The report's own PASS/FAIL column |
| Layout matched the vendor's figure | The figure is captioned as an example whose "quantities will vary" | Treating a drawing as a specification | Reading the paragraph around the diagram before restructuring |

Three of the four printed a clean summary. Every one was found by looking at the
modelled thing rather than the run's output.

---

## What transfers

**A tool reporting success is not evidence of success.** See the table above.

**Declare intent explicitly, including absence.** An omitted field means "don't care"
and can never be checked. An explicit null is a claim the reconciler can test — the
whole difference between drift that surfaces and drift that doesn't.

**A preview must match the apply, or it stops being read.** Any gap between what the
dry run reports and what a real run does trains the operator to skip the dry run.
Then the safety mechanism is decoration.

**Status labels must not need interpreting.** An early verdict column used `CHECK`,
which reads as easily as "checked, fine" as it does "look at this" — and was misread
on first sight. `PASS`/`FAIL` costs nothing and cannot be misread.

**Verify before you restructure.** Confirming that a vendor diagram was an example
rather than a standard took ten minutes and prevented three hours of work toward a
claim that would not have survived review.

**Build one by hand before automating any of it.** The hand-built rack was the
reference every later check was made against — and the generator's fidelity was
proved the moment it ran over that rack and changed nothing.

---

## The limit that's still there

This generator creates and updates. It **never deletes**.

Nothing removes an object the spec has stopped declaring, so a rack dropped from the
YAML lingers in the database indefinitely. A full reconciler — Terraform, Ansible —
would destroy it.

That is a real gap, named here rather than discovered later by someone assuming the
spec is the complete truth. **Convergence is guaranteed in one direction only:**
everything declared will exist and match. Nothing says the reverse.
