# Bioinfo Harness

A pluggable multi-agent harness for bioinformatics — inspired by
[Munder Difflin](https://munderdiffl.in/), rebuilt around scientific pipelines
instead of a code repository.

Munder Difflin wraps agent CLIs as per-person "clone" nodes that share a memory
palace, talk over file mailboxes, and run inside isolated git worktrees. The
same shape works for a lab, with one substitution: **the substrate is not a repo,
it is a capability registry**. Any tool becomes available to the agents by
dropping a YAML manifest into `capabilities/` — an NGS aligner, an MD engine, a
docking program, a Python function. The core knows about ports, types, runtimes,
budgets and provenance. It never knows about a specific tool.

```
                    ┌──────────── hive ────────────┐
  goal ──▶ PI ──▶ planner ──▶ curator ──▶ [orchestrator] ──▶ validator ──▶ analyst ──▶ scribe
                    │  mailboxes · blackboard · memory palace · event log  │
                    └──────────────────────────────┘
                                     │
                          capability registry (YAML)
                    ┌────────────────┼────────────────┐
                  NGS           structural          general
            fastqc, fastp     pdb_fetch, gmx      multiqc, metrics
            bwa, bcftools     vina, MDAnalysis
```

## Why this shape

| Munder Difflin | Bioinfo Harness |
|---|---|
| Clone node wrapping an agent CLI | Role agent wrapping an agent CLI *or* a deterministic function |
| Isolated git worktree | Isolated run workspace + hashed provenance ledger |
| MemPalace shared memory | Markdown per-agent memory, mined into an FTS index |
| Atomic-file mailboxes, single committer | Same — the orchestrator is the only executor |
| Circuit breaker, token budgets | Budgets, breakers, and a steer → constrain → stop ladder |
| Repo as substrate | **Capability registry as substrate** |

## Install

```bash
pip install -e .            # core, needs only PyYAML
pip install -e '.[md,dev]'  # + MDAnalysis, pytest
```

## Try it without installing a single bioinformatics tool

```bash
bioharness doctor                       # what is actually runnable here
bioharness caps --domain structural     # browse the registry
bioharness plan fastq vcf -d ngs        # route between two data types
bioharness plan pdb trajectory -d structural
```

`plan` walks the type graph built from the installed manifests — `fastq → bam →
vcf` for NGS, `pdb → topology/coordinates → trajectory` for MD — so routing is a
property of what you have installed, not a hardcoded pipeline.

Every workflow runs in `--dry-run` first. It resolves and prints the exact
command for **every** step, chaining predicted outputs through the whole DAG,
without executing anything:

```bash
bioharness run wgs-variants --dry-run \
  --set reads=/data/sample_R1.fastq.gz \
  --set reference=/refs/GRCh38.fa

bioharness run md-stability --dry-run --set pdb_id=1AKI
```

## The dashboard

```bash
bioharness ui                 # opens http://127.0.0.1:8765
bioharness ui --no-open --port 9000
```

### floor

The landing view is a pixel-art lab floor drawn on a canvas, with the labels and
status chips on an SVG layer above it so text stays crisp at any zoom. Each
agent is seated at the station its role implies — PI and planner in the planning
office, curator in the sequencing bay, validator at the QC bench, analyst in the
structure lab, scribe at the write-up desk — and rooms with nobody in them are
dimmed and labelled `unstaffed`.

The floor is one continuous building. Every room is a sealed enclosure — back
wall band, both side walls, and a front wall broken by exactly one doorway —
including the waiting lobby along the bottom. Rooms open onto one of two
corridor lanes, and the lanes are joined by the vertical gaps between the room
columns. **The staff are
animated and the floor is interactive:**

- agents sit at their stations when idle, and stand up to walk
- a routed message becomes an errand — the sender walks to the recipient's
  desk, pauses there, then heads back
- **nobody walks through a wall**: a route slides across to its own room's
  doorway, steps out, follows a corridor and enters the target room by *its*
  doorway. `tests/route_check.mjs` samples every room-to-room route and fails
  if any point lands inside a wall
- hover highlights an agent; click one to select it (the command center and the
  message box follow the selection)
- with an agent selected, click anywhere on the floor to send them walking there

Movement is driven by real events from the hive, not a screensaver: if nothing
is happening, nobody moves.
 Rooms are declared as
pixel rects in `ROOMS` (in `bioharness/ui/app.js`) with a floor tile, a list of
equipment lining the back wall and a list of furniture standing on the floor —
so adding a room or moving a role is a data edit, not a rendering change.
Artwork lives in `bioharness/ui/assets/`; see the note there on regenerating it.

Status is derived, never stored: an agent is `working` while it has acted in the
last 25s, `awaiting` while it holds unread mail, `blocked` if its last event said
so, `idle` otherwise. Recent inter-agent messages are drawn as dotted wires with
a travelling dot, so you can watch a mission move across the floor.

The command center on the right carries the live event stream, the worker
roster, memory-palace search, the current run's steps, and its DAG. The box at
the bottom queues a message straight into an agent's mailbox.

### runs / hive / caps / cost

- **runs** — the pipeline as a DAG coloured by live status, the step table,
  parameters, SHA-256 artifact list, and the full provenance ledger. Click any
  step for its `stdout` / `stderr`.
- **hive** — the roster with autonomy levels, each agent's pending mail and
  `memory.md`, the blackboard, the event log, and memory-palace search.
- **caps** — the registry with a live availability probe and the reason each
  unavailable tool is unavailable.
- **cost** — token accounting per agent, plus the run's budget and breaker state.

The command center's **staff** tab hires and dismisses agents from the browser:
pick a name, role, provider, model and autonomy level, and the new agent walks
onto the floor. Providers that cannot run here are shown disabled with the
reason. Dismissing archives the agent's memory and mail under `former/` rather
than deleting it.

### What the dashboard may do

It is loopback-bound and reads files for everything. There is exactly **one**
write path: `POST /api/message`, which drops a message into an agent's mailbox.
The recipient must already be enrolled and the topic must be one the roster
actually handles, so the box cannot be used to inject arbitrary work. Reads
never mutate: the hive is *peeked* rather than consumed, so watching a mission
never eats its mail.

Point it at a cluster run over `ssh -L 8765:127.0.0.1:8765`. Stdlib
`http.server` plus three static files — no JS toolchain on the analysis box.

## Run the agents

```bash
bioharness staff hire ada --role analyst --provider claude-api --model claude-opus-5
bioharness staff hire quill --role scribe            # deterministic, free
bioharness staff list
bioharness mission "call variants in this WGS sample" --from-type fastq --to-type vcf -d ngs
```

With no `--roles`, a mission staffs itself from whoever is hired, and each agent
uses **its own** provider — so an API-backed analyst and a deterministic curator
work the same mission. `--roles` overrides the roster for one run.

### Providers

| Provider | What it is | Cost |
|---|---|---|
| `builtin` | Deterministic logic — type-graph planning, path/type curation, QC gates | free |
| `claude-api` | The Anthropic Messages API via the official SDK | metered, real usage |
| `gemini-api` | Google's Gemini API with an AI Studio key | metered; tokens recorded, dollars not priced |
| `ollama` | A model running locally through Ollama | free; nothing leaves the machine |
| `claude`, `codex`, `gemini`, `qwen`, … | An agent CLI you already have logged in | your subscription |

The Claude API provider defaults to **`claude-opus-5`** with adaptive thinking,
and sets `effort` per role (planner and analyst reason harder than the curator).
It reads real `usage` off each response, so the cost ledger is measured rather
than estimated — cache reads at 0.1x input, cache writes at 1.25x.

Credentials resolve the way the SDK resolves them: `ANTHROPIC_API_KEY`, then
`ANTHROPIC_AUTH_TOKEN`, then a stored `ant auth login` profile. An unset API key
does not mean there are no credentials, and `bioharness doctor` reports which
source is in play. **If a provider is unavailable the agent still runs** — it
falls back to its deterministic behaviour and says so, rather than failing the
mission.

### Gemini

```bash
export GEMINI_API_KEY=...       # or GOOGLE_API_KEY / GOOGLE_GENAI_API_KEY
bioharness staff hire nova --role analyst --provider gemini-api
```

Model ids are discovered from the API rather than hardcoded, so the hire form
offers exactly what your key can call and a retired model id cannot rot in the
source. Naming no model picks the cheapest available tier.

**A Gemini API key is not an Antigravity key.** Google retired the standalone
Gemini CLI in June 2026 in favour of the Antigravity CLI, which authenticates
with its own `AV_API_KEY` from the Antigravity dashboard and does not accept a
Gemini API key. The two are separate providers here: `gemini-api` for the API,
`antigravity` for the CLI (wrapped like any other agent CLI). The Antigravity
IDE — the VS Code fork — is an editor and cannot be driven headlessly.

### Local models

```bash
ollama serve &                       # if it is not already running
bioharness staff hire planner --role planner --provider ollama --model qwen2.5:3b
```

Local inference costs nothing per call and never sends sequence data or sample
metadata off the machine, which matters more here than in most domains. The
provider is stdlib-only — Ollama's HTTP API is small enough that a client
library would be more dependency than it is worth.

Small models get workflow JSON *roughly* right, so two things guard against
that: replies truncated mid-structure are repaired rather than discarded, and
a model-authored workflow is validated before it is published — invented
capabilities are dropped, `needs` entries that do not name a real earlier step
are dropped, and what survives must topologically sort. A plan that cannot be
made runnable is never published.

Every role can be staffed more than once: hire two curators and they take
neighbouring desks in the sequencing bay.

## Adding a tool

This is the whole integration story. Drop a manifest into `capabilities/`:

```yaml
capabilities:
  - id: samtools_flagstat
    domain: ngs
    version: "1.19"
    summary: Alignment summary metrics from a BAM
    runtime: {kind: docker, image: "quay.io/biocontainers/samtools:1.19--h50ea8bc_0", binary: samtools}
    command: "sh -c \"samtools flagstat {{ inputs.bam }} > {{ outdir }}/flagstat.txt\""
    inputs:  [{name: bam, type: bam}]
    outputs: [{name: metrics, type: table, glob: "flagstat.txt"}]
    resources: {cpu: 2, mem_gb: 4}
    citation: "Danecek P, et al. GigaScience. 2021."
```

The tool is now searchable, routable by the planner, executable by the
orchestrator, and citable by the scribe. No code changes.

**Runtimes:** `binary` · `conda` · `docker` · `apptainer` (HPC) · `python`
(in-process function) · `agent` (delegate to an LLM CLI). Declare `requires:`
for Python capabilities whose heavy dependency is imported lazily, so `doctor`
reports availability honestly instead of failing mid-run.

## Workflows

```yaml
name: wgs-variants
params: {reads: null, reference: null, threads: 8}
steps:
  - id: align
    capability: bwa_mem
    with:  {reads: "{{ params.reads }}", reference: "{{ params.reference }}"}
    params: {threads: "{{ params.threads }}"}
    retries: 1
  - id: call
    capability: bcftools_call
    with: {bam: "{{ steps.align.outputs.bam }}", reference: "{{ params.reference }}"}
    needs: [align]
```

- `needs:` — DAG edges, topologically sorted, cycles rejected up front
- `scatter:` — fan one step out per item (see `workflows/dock-screen.yaml`)
- `when:` — guard a step on a parameter, compared as data, never `eval`'d
- `retries:` — bounded, and still subject to the circuit breaker

## Guardrails

Bioinformatics fails expensively — a mis-specified MD run burns GPU-days, a
looping agent burns tokens. Every execution path checks guardrails first.

- **Budget** — steps, wallclock, tokens, USD, CPU-hours, GPU-hours
- **Circuit breaker** — a capability that fails repeatedly stops being retried
- **Intervention ladder** — a misbehaving agent is *steered*, then *constrained*
  (autonomy dropped to propose-only), and only then *stopped*; each rung is logged
- **Autonomy levels** — `read_only` · `suggest` · `bounded` · `full`

## Provenance

Every run writes an auditable tree:

```
runs/<run_id>/
  run.json        workflow, params, host, platform, python version
  ledger.jsonl    append-only: every task, every artifact, SHA-256 hashed
  summary.json    status, timings, resolved commands, guardrail state
  steps/<id>/     the step's cwd, stdout.log, stderr.log, outputs
  results/        promoted final outputs
```

The scribe writes the Methods section from that ledger — tool versions and the
parameters actually used — so the write-up cannot drift from the run.

## The hive

```
.bioharness/hive/
  agents/<name>/identity.json  memory.md  inbox/  outbox/  archive/
  blackboard/<key>.json        shared facts
  events.jsonl                 append-only event log
  palace/index.sqlite          FTS index over all agent memory
```

Everything is a plain file you can `cat` — which matters when a run is
half-finished on a cluster node at 3am, and lets the harness work over NFS/Lustre
where a socket would not. Messages are written tmpfile-then-`os.replace`, so a
reader never sees a partial message. Only the router moves mail between agents,
and only the orchestrator executes tools — the single-committer rule that keeps
parallel agents from colliding.

```bash
bioharness status                 # roster, pending mail, cost ledger
bioharness recall "coverage gate" # search the memory palace
```

## Tests

```bash
pytest -q     # 149 passed
```

The suite builds its capabilities out of `sh`, `tr` and `echo`, so it proves the
harness mechanics — binding, DAG ordering, scatter, retries, provenance hashing,
budgets, breakers, mailbox routing, and the dashboard API — on any machine, with no bioinformatics tool
installed.

## Status

Working core and dashboard. Not yet built: HPC submission (Slurm/SGE executor
backends) and encrypted cross-machine transport for the mailbox layer.

MIT.
