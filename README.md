# Reference GPU Pod — DCIM Model & Commissioning Package

A NetBox model of one **NVIDIA DGX SuperPOD scalable unit** — 32× DGX H100 across
12 racks — built from NVIDIA's published reference architecture, with a generator
that reproduces the whole model from a YAML spec.

> **Scope honesty.** This is a design and documentation exercise: a paper model of a
> published reference architecture. Nothing here was deployed or operated, and it is
> not affiliated with NVIDIA. Figures are labelled **sourced** or **assumed**
> throughout, and the distinction is kept deliberately visible.

**Status: complete.** 12 racks, 92 devices, 970 cables, 359.6 kW, N−1 PASS on every
rack. Everything rebuilds from `spec/pod.yaml` by running one script.

---

## What's here

| Path | Contents |
|---|---|
| `spec/pod.yaml` | Declarative description of the pod — the desired end state |
| `scripts/build_pod.py` | Idempotent generator; reconciles NetBox against the spec |
| `scripts/netbox_client.py` | Shared connection helper (handles both NetBox auth schemes) |
| `scripts/check_connection.py` | Preflight — verifies reachability and **write** scope |
| `scripts/show_choices.py` | Prints the type slugs the live instance accepts |
| `scripts/export_reports.py` | Generates the power report, cable schedule and rail map |
| `docs/design-notes.md` | What is sourced, what is a choice, what is a guess |
| `docs/commissioning-checklist.md` | Site readiness through handover |
| `exports/` | Generated CSVs — power report, cable schedule, rail map |
| `env-example/` | `.env` and compose override to copy into `netbox-docker/` |

---

## Running it

```bash
git clone https://github.com/netbox-community/netbox-docker.git
cp env-example/.env env-example/docker-compose.override.yml netbox-docker/
cd netbox-docker && docker compose up -d
docker compose exec netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py createsuperuser
```

NetBox comes up on <http://127.0.0.1:8000/> — bound to loopback deliberately, since
the upstream example publishes on all interfaces. Pinned to NetBox **v4.7** via
netbox-docker **5.1.0**, Postgres 18, Valkey 9.1.

Then create an API token in the UI and:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
printf 'NETBOX_URL=http://127.0.0.1:8000\nNETBOX_TOKEN=<your token>\n' > .env
python scripts/check_connection.py
python scripts/build_pod.py --dry-run
python scripts/build_pod.py
```

`build_pod.py` is idempotent — run it as often as you like. It reads current state
fresh each time rather than tracking what previous runs did, so an interrupted run
is fixed by running it again, with no cleanup step.

---

## The pod

12 racks in one scalable unit:

| Racks | Role | Contents |
|---|---|---|
| C01–C08 | Compute | 4× DGX H100 at U3/U11/U22/U30, 3× rPDU at U42/44/46 |
| N01–N02 | Network | 8× QM9700 compute leaf, 4× spine, storage fabric |
| M01 | Management | UFM pair, SN4600C in-band, SN2201 out-of-band |
| S01 | Storage | Storage nodes and storage fabric leaf |

### Sourced figures

| | Value | |
|---|---|---|
| DGX H100 | 8U, 10.2 kW, 130.45 kg, 6 PSUs in 4+2 | [user guide][dgx] |
| | 8× NDR400 InfiniBand per system | [components][comp] |
| Scalable unit | up to 32 DGX H100; 8 leaf + 4 spine | [architecture][arch] |
| Rack density | 4 systems/rack → >40 kW/rack | [architecture][arch] |
| Racks | EIA-310, ≥48U, 800×1200 mm recommended | [electrical][elec] |
| Power scheme | 415 VAC, 60 A, 3-phase, N+1, ≥3 sources/rack | [electrical][elec] |
| | each source sized for 50% of peak load | [electrical][elec] |
| QM9700 | 1U, 64× NDR400, 747 W passive / 1720 W active | [QM97x0 specs][qm] |
| SN4600C | **2U** (88 mm), 64× QSFP28 100GbE, 466 W | [SN4000 specs][sn4] |
| SN2201 | 1U, 48× 1GbE + 4× QSFP28, 98 W | [SN2201 specs][sn2] |

| UFM appliance | 2U (88 mm), 2 PSUs, InfiniBand + OOB ethernet | [UFM specs][ufm] |
| Rack profile | servers from U3, 3U airflow gap, horizontal rPDUs at top | [infrastructure][infra] |

### Assumed, not sourced

Storage node model and count, the 2 storage + 2 in-band NICs per DGX, SN2201 dual PSU,
UFM power draw, rPDU height, and all cable lengths. Full list with reasoning in
**[docs/design-notes.md](docs/design-notes.md)**.

That file also separates what NVIDIA *prescribes* from what it explicitly leaves open.
The compute side is specified in detail; the management-rack figure carries the caveat
that "sizes and quantities will vary depending upon models used." The infrastructure
choices here are choices, and are labelled as such rather than presented as conformance.

---

## Design decisions

**Leaf switches end-of-row, not top-of-rack.** Rail-optimised topology requires rail
*n* from all 32 nodes to land on the same leaf, which makes ToR structurally
impossible. This is also what brings the 30 m InfiniBand guidance into play.

**QM9700 budgeted at 1720 W, not 747 W.** NVIDIA publishes 747 W *with passive
cables*; end-of-row placement exceeds passive copper reach, so this pod runs optical
and the active-cable maximum is the honest number. The topology decision propagates
directly into the power budget.

**Three power sources per rack, not an A/B pair.** Four DGX H100 at peak draw
**40.8 kW**, but no single circuit option in NVIDIA's table exceeds 32.8 kW. That gap
is why the guide specifies a minimum of three sources at 50% sizing rather than two.

**800 mm cabinets, not the 600 mm minimum.** With 8 NDR400 rails per node plus
storage, in-band, out-of-band and three power feeds, 600 mm is impractical — and 800
is NVIDIA's own recommendation.

**Power modelling rule.** `allocated_draw` = total ÷ PSU count (normal operation);
`maximum_draw` = total ÷ PSUs required (worst case). For the DGX H100's 4+2 that is
1700 W and 2550 W per supply. Budget against allocated; size circuits against maximum.

**UFM InfiniBand ports left uncabled.** A strictly 1:1 non-blocking fabric consumes
every port — 32 down and 32 up per leaf, and 8 leaves × 8 links fills all 64 spine
ports — so there is nowhere to attach a management appliance. Real deployments reserve
fabric ports for this at the cost of slight oversubscription. Inventing a free port
would have produced a model that looks complete while quietly breaking the property
the topology exists to provide.

---

## Results

From `exports/`, regenerated by `scripts/export_reports.py`:

| | |
|---|---|
| Pod load | **359.6 kW**, 91% of it GPU |
| Compute rack, steady state | 13,600 VA of 34,501 VA per feed — **39.4%** |
| Compute rack, one source lost | 20,400 VA per feed — **59.1%**, **N−1 PASS** |
| Network racks | ~10% — the fabric is not the constraint |
| Cables | 970, none exceeding the 50 m optical limit |
| Rail map | 8 rails, one leaf each, 32 nodes each — **all PASS** |

**The N−1 figure is the validation.** On loss of one of three sources each survivor
carries **20.4 kW** — NVIDIA's published peak-server-demand-per-circuit figure, reached
independently by walking the model's own cable topology rather than copied from the
document.

**One discrepancy, reported rather than smoothed over:** NetBox computes the
415 V / 60 A three-phase feed at **34,501 VA**; NVIDIA's electrical table lists the
same circuit at **32.7 kW**. About 5%, from a different derating assumption. It does
not change the N−1 verdict — every rack passes against either figure — but it should
be resolved against a facility's own standard before anything is ordered.

---

## Notes on NetBox itself

Two behaviours that cost real time, recorded because they aren't obvious:

**A pass-through power port aggregates downstream load only when both `maximum_draw`
and `allocated_draw` are empty.** Setting `maximum_draw` on an rPDU's input makes
NetBox use the declared value instead of computing from what's plugged in, so the feed
reads 0 VA behind a perfectly healthy "Reachable" cable. Supplying *more* data breaks
the calculation.

**Power outlets must have their parent power port set**, or there is no path from a
device PSU up to the feed. Fails the same silent way.

Also worth knowing: the REST API does not expand `[1-64]` name ranges — that is a
UI-form feature only. And NetBox 4.7 issues `nbt_<key>.<token>` credentials requiring
the `Bearer` scheme rather than pynetbox's default `Token`.

[dgx]: https://docs.nvidia.com/dgx/dgxh100-user-guide/introduction-to-dgxh100.html
[comp]: https://docs.nvidia.com/dgx-superpod/reference-architecture-scalable-infrastructure-h100/latest/dgx-superpod-components.html
[arch]: https://docs.nvidia.com/dgx-superpod/reference-architecture-scalable-infrastructure-h100/latest/dgx-superpod-architecture.html
[elec]: https://docs.nvidia.com/dgx-superpod/design-guides/dgx-superpod-data-center-design-h100/latest/electrical.html
[qm]: https://networking-docs.nvidia.com/qm97x0hw/specifications
[sn4]: https://networking-docs.nvidia.com/sn4000hw/specifications
[sn2]: https://networking-docs.nvidia.com/sn2201hw/specifications
[ufm]: https://networking-docs.nvidia.com/ufmenterprisendrhwum/mechanical-installation
[infra]: https://docs.nvidia.com/dgx-superpod/design-guides/dgx-superpod-data-center-design-h100/latest/infrastructure.html
