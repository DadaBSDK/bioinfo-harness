# Bioinfo Harness

A pluggable multi-agent harness for bioinformatics, built around scientific
pipelines.

Agent CLIs are wrapped as named lab members who share a memory palace, talk over
file mailboxes, and work inside isolated run workspaces. **The substrate is a
capability registry**: any tool becomes available to the agents by
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
bioharness ui --attend        # …and act on what you send from the browser
bioharness ui --no-open --port 9000
```

**A message you send from the dashboard does nothing on its own.** Posting only
drops mail into an agent's mailbox; something has to route it and tick the
roster, or it sits there and every agent reads `awaiting`. Either run the
attendant beside the dashboard with `--attend`, or in its own terminal:

```bash
bioharness attend             # route mail and tick the roster until stopped
bioharness attend --interval 5 --max-usd 2
```

The server never acts on what it receives either way: `--attend` is the same loop
running in a thread of the same process, never driven by an HTTP request.

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
columns; the corridors are laid in their own tile, edged and worn down the
middle, so circulation reads as circulation rather than as a gap between boxes.

What stands in each room is the instrument list from `agents/lab.py`, drawn with
the nearest sprite the atlas has — the sequencer, thermocycler and -80 in the
sequencing bay, two rows of racks and the head-node terminal in the cold aisle,
the microscope and gel dock at the QC bench. Every room carries a nameplate in
its own accent colour, wears that colour again on its threshold mat, and has a
work surface its people stand at. A staffed room is lit from its own strip
lights; an empty one has the lights off and goes cold and blue, which reads at a
glance across six rooms. **The staff are
animated and the floor is interactive:**

- agents sit at their stations when idle, and stand up to walk
- a routed message becomes an errand — the sender walks to *the recipient*,
  pauses with them, then heads back. Walking to their desk instead had two
  agents swap rooms whenever they replied to each other
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

### chat

The roster's messages read back as a conversation: who spoke, who they spoke to,
what they actually said, and when. Peer questions and answers appear inline with
the functional traffic, so a mission reads as an exchange rather than as a log of
topics. The same utterances surface on the **floor** as speech bubbles over the
agent who said them, fading after a few seconds — the floor for what is happening
now, the chat tab for reading the whole thing back.

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
actually handles, so the box cannot be used to inject arbitrary work. `peer.ask`
is one of those topics, so the composer can put a question to an agent and read
the answer in the chat tab. Reads
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

### Orders, and the brief every order produces

The operator is the one voice in this lab that cannot be ignored.

```bash
bioharness order "take this sample from FASTQ to called variants"
bioharness order "check the reference genome" --to iris
bioharness brief latest          # or a thread id
```

An order goes to the whole roster by default. Three rules make it an order
rather than a suggestion:

- **It is read first.** An agent sorts the operator's mail to the front of its
  inbox, so an order never waits behind a conversation about the coffee rota.
- **It is never dropped.** Peer and social topics already bypassed a role's
  `subscribes`; so does anything the operator sends. A message to a scribe on a
  topic no scribe handles used to vanish, which is exactly what "they don't
  listen" looks like from outside.
- **Everyone owes it a verdict**: `will do`, `will do with caveats`, or
  `cannot do`, with what they will do about it, what they need, and the one risk
  they would flag. The PI answers *and* puts the lab to work; the others answer
  and get on with their part.

Two things an order still cannot do, because they would make the lab useless to
the person giving the order: state something as fact that the data does not
show, and pretend a check passed when it did not. An agent that hits one of
those answers `cannot do`, names the obstacle, and offers the nearest honest
thing instead. The autonomy ladder is unchanged as well — an order cannot make a
`read_only` agent execute.

**An order can carry its own parameters.** Anything written as `key=value` in
the order text travels with the mission to the step that declared it, so the
lab's question can be answered with one more order rather than a conversation:

```bash
bioharness order "fetch the reads and stop there: accession=SRR000001 max_reads=500"
```

Two capabilities exist to make "fetch the data online" mean something —
`ena_fetch` (reads from ENA/SRA by run accession) and `ref_fetch` (a reference
FASTA from a URL). Without them the type graph began at a `fastq` file somebody
had already downloaded by hand, so an order to fetch and run was unroutable and
the lab could only discuss it. They are Python capabilities rather than shell
one-liners: a fetch is where a pipeline meets the outside world, and a dead
mirror or an unreleased accession deserves a sentence the operator can act on
rather than `exited 2`.

**Every order becomes a PDF.** Once the room goes quiet, the attendant writes a
verdict brief: the order verbatim, every agent's verdict in its own words with
its needs and risks (refusals first), anyone who spoke but never answered, what
was executed under the order, and what it cost. It lands in `runs/briefs/`, is
linked in the **chat** tab and listed under **briefs** in the command center, and
is served at `/api/brief/<thread>`.

```bash
bioharness attend --brief-after 20    # seconds of quiet before a brief is written
bioharness attend --no-brief          # do not write them
```

The whole document is reconstructed from messages on disk, so a brief can be
rendered again months later from a hive directory alone. It is written by
`tools/pdf.py` — about two hundred lines of standard library, base-14 fonts, no
dependency and no headless browser, for the same reason the dashboard needs no
JS toolchain: a bioinformatics box should not have to install a document stack
to produce a page of text.

Threading is what makes this possible: a message that answers nothing starts a
thread of its own, and everything said in reply carries that id. One order,
twenty-eight messages, one brief.

### Agents talking to each other

Every agent can put a question to any other, by name or by role:

```python
self.ask_peer("curator", "Which of these inputs would you refuse?", steps=chain)
```

The base class answers on behalf of whatever role received it, so a scribe that
only subscribes to `report` still has an opinion when asked. Three rules keep an
open channel from becoming an unbounded one:

- **A reply is never answered back.** An exchange is two messages deep by
  construction, which is what stops two agents talking until the budget is gone.
- **Roles are resolved to names.** A mission staffed from the hive has a planner
  called `ada`, not one called `planner`; `hive.whois()` maps one to the other,
  and asking for a role nobody fills is recorded as `agent.no_peer` rather than
  raised.
- **Hops are capped** (`MAX_HOPS`), and the mission's round limit and budget
  guardrails apply to peer talk exactly as they do to tool execution.

Who starts a conversation, and why:

| Who | Asks | About |
|---|---|---|
| PI | whoever the job needs | one order per role, in the PI's own words |
| planner | curator | which inputs the curator would refuse, before compute is spent |
| validator | analyst | which artefact explains a gate that just failed |

### They are labmates, not job functions

Every agent has a persona: a temperament, a way of talking, something it cares
about that has nothing to do with the science, and something that reliably
annoys it. It is derived from the agent's name with a stable hash — `ada` is the
same person tomorrow and on the cluster node — and written into `identity.json`
the first time it is used, so a personality does not drift when the trait tables
in the source change.

```bash
bioharness staff hire mira --role analyst --provider antigravity
# hired mira as analyst (antigravity/gemini-3.8-flash-low)
#   world-weary but kind; into a half-finished PhD in something unrelated;
#   cannot stand unlabelled tubes
```

The persona goes into the system prompt alongside the role, so it colours the
work too — but it is explicitly not allowed to soften a scientific judgement.
An agent can be funny and still refuse a bad input.

**A new hire gets welcomed.** When someone joins while the lab is running, every
other agent goes over and says hello in its own voice, and the newcomer answers
each of them. Nobody greets the people who were already in the room when the
lights came on.

**A quiet room gossips.** When there is no work in flight, the attendant lets one
agent start a conversation about nothing — weighted by how chatty its persona is.
It runs two turns and peters out, the way a real one does when someone's run
finishes. Off-topic talk costs tokens like anything else, so it is on a timer:

```bash
bioharness attend --social-every 45     # more talkative
bioharness attend --no-social           # work only; greetings still happen
```

Social topics (`greet`, `greet.reply`, `smalltalk`) bypass a role's
`subscribes`, because being a colleague is not one role's job, and they never
produce a `.result` — a chat is not a work item. They appear in the **chat** tab
in a lighter style, so gossip is legible as gossip.

### One lab, one map

Agents used to address each other by role and furnish the building themselves —
a third-floor printer, a cold room, a machine nobody else had heard of. Now
`agents/lab.py` is the single answer to *what is here and where is it*: seven
rooms, the instruments in each, and which role works where. Every agent's prompt
carries the map, the line **"there is no other room, floor or instrument — do
not invent one"**, and a roster of colleagues by name with the room each sits in.

```
Planning Office   pi, planner    whiteboard, two dual-monitor desks, wall clock…
Sequencing Bay    curator        benchtop sequencer, -80, thermocycler, TapeStation…
QC Bench          validator      microscope, plate reader, the good centrifuge…
Compute Cluster   runner         four racks, head-node terminal, tape library, UPS…
Structure Lab     analyst        GPU workstation, molecular viewer, the window seat…
Write-up Desk     scribe         the printer that jams on figures, reference shelf…
Waiting Lobby     operator       sofas, the coffee machine that eats coins…
```

The room ids match `ROOMS` in `ui/app.js`, and a test fails if they drift — the
lab an agent talks about has to be the lab you can watch. Personas draw their
haunt from their own room, so *"you'll find me by the sequencer"* is true, and
every pet peeve points at something the building actually has.

On the floor, a message makes the sender walk **to the person, not to their
desk**. Targeting the desk had two agents swap rooms whenever they replied to
each other — the PI marching to the lobby while the human marched to the
planning office, passing in the corridor. Whoever is spoken to now waits to be
reached, and two people already standing together just talk.

### The invented lab is not evidence

Give agents a shared world and they will furnish it: within an hour of the first
gossip, this roster had a -80 freezer with a broken alarm, a coin-eating coffee
machine, and a plan to silence the alarm with a wrench. That is harmless — no
capability in the registry touches hardware, and none of it exists — but it does
not stay harmless on its own, because an agent's memory is fed back to it as
context. One analyst opened a genuine QC answer with *"Someone left the latch
loose on the -80 again"*: the joke was stored as memory, recalled on the next
question, and repeated. A fiction that reinforces itself one turn at a time ends
up as a fabricated artefact in a Methods section.

Three rules keep the two apart:

- **Separate memories.** Social talk is written to `social.md` and indexed as
  kind `social`; work recall reads only `memory` and `note`. It is still the
  agent's memory — it just cannot come back as evidence.
- **A guard on every work prompt.** Answer the question, no scene-setting, and
  state only what the files, metrics and ledger show. Small talk is exempt: it
  is *supposed* to be about the imaginary freezer.
- **Own memory only.** `think()` primes an agent with its own notes. The palace
  still holds the whole lab's memory and `bioharness recall` searches all of it,
  but priming one agent with another's recollections had them answering in each
  other's voices about work they never did.

The personas are told the same thing directly: the lab around them is banter,
they have no hands and no instruments, and they never claim to have touched
anything.

### What the PI does with a goal

The PI reasons about the goal and writes each staffed role its own order —
`{role, instruction}` under an enforced JSON schema — rather than running a fixed
handoff. Orders are then checked against reality before anything is sent:

- A role nobody fills is dropped and recorded as `agent.no_peer`; an invented
  role never reaches a mailbox.
- Each role is tasked at most once, and only with the topic that actually gives
  it work (`planner → plan.request`, `curator → data.validate`, and so on).
- A role whose inputs do not exist yet is **asked** instead of tasked. A
  validator handed `qc.check` with no metrics returns a meaningless pass, so it
  gets a question until there are numbers to gate.
- With no provider, or on a model that answers with prose instead of JSON, the
  PI falls back to the standard split: plan it, and ask everyone else what they
  need. That is not a degraded mode — it is what a PI does with any goal.

When the plan comes back, the PI hands the planned steps to the scribe so a
Methods draft exists before a byte is processed; if no route was found, it asks
the curator and analyst what capability is missing rather than going quiet.

An agent with no LLM provider still answers — it says so plainly instead of
going quiet, because silence reads as having no opinion.

**They answer, they do not decline.** An early version of the peer prompt ended
with *"say plainly if this falls outside your remit"* — and 32 of 46 answers
opened by doing exactly that, because a narrow role description plus an offered
escape hatch produces a refusal every time. The instruction now says the
opposite: lead with the most useful thing you can say from where you sit, answer
your part of a question that spans roles and name who should take the rest, and
refuse only when answering would mean inventing data you do not have — saying
which data is missing. The personas carry the same line: colleagues ask you
things next to your job, and helping anyway is what everyone in a working lab
does.

### Providers

| Provider | What it is | Cost |
|---|---|---|
| `builtin` | Deterministic logic — type-graph planning, path/type curation, QC gates | free |
| `claude-api` | The Anthropic Messages API via the official SDK | metered, real usage |
| `gemini-api` | Google's Gemini API with an AI Studio key | metered; tokens recorded, dollars not priced |
| `antigravity` | The Antigravity CLI (`agy`), which fronts Gemini, Claude and GPT-OSS | your Antigravity plan |
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
source. Naming no model picks a flash tier.

The key is sent as an `X-goog-api-key` header, never in the query string —
URLs end up in proxy logs, access logs and shell history. Thinking tokens
(`thoughtsTokenCount`) are counted as output: on a flash model a two-token
answer routinely carries ~190 thinking tokens, so ignoring them would
under-report usage by an order of magnitude. Overload and rate-limit responses
(429, 5xx) are retried with backoff and do not take the agent offline; a
rejected key or unknown model is permanent and does.

### Antigravity

```bash
bioharness staff hire nova --role planner --provider antigravity --model gemini-3.8-flash-low
```

Point the whole roster at one backend without losing anyone's memory:

```bash
bioharness staff assign --provider antigravity --model gemini-3.8-flash-low
bioharness staff assign planner ada --provider antigravity --model gemini-3.8-flash-high
```

`agy` gets a dedicated provider rather than the generic CLI wrapper, because
`--output-format json` returns real token usage — input, output, thinking and
cache reads — instead of a character-count estimate, and because one credential
fronts several model families (Gemini 3.x, Claude Sonnet/Opus 4.6, GPT-OSS).
The CLI authenticates itself; the harness never touches that credential.

Two quirks the harness absorbs. With `--json-schema` the validated object comes
back in `structured_output`, and `response` may be empty or hold fenced prose —
the structured field wins. And the `-medium`/`-high` tiers sometimes finish a
schema-constrained turn agentically and emit nothing at all, burning real
thinking tokens; an empty reply is retried once without the schema. That second
call is why a high-tier planner costs roughly twice a low-tier one — `-low`
answers the schema directly and is the better default for planning.

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

## Authority

An agent's `autonomy` used to be a stored label: nothing consulted it, so
`read_only` restrained nothing and `full` granted nothing — and no agent could
execute at all, because the orchestrator was only ever built by `bioharness run`.
Both are now real.

| Level | May execute |
|---|---|
| `read_only` | nothing — plans and inspects |
| `suggest` | nothing without a human; the step is refused with that reason |
| `bounded` | only what is in its declared `--scope` (capability ids, or `domain:<name>`) |
| `full` | anything in the registry |

```bash
bioharness staff hire rex --role runner --autonomy full
bioharness staff hire rex --role runner --scope fastqc fastp domain:ngs
bioharness staff assign --provider antigravity --autonomy full    # the whole roster
```

The check lives in the orchestrator, because that is the single committer: gate
the one place that starts work and no agent can slip a step past it. A refusal is
not an exception — the step is recorded `blocked` with the reason, in the run's
own ledger, so `runs` shows exactly what was refused and why:

```
metrics -> blocked | rex is read_only: may plan and inspect, never execute
```

A `bounded` agent with no scope may run nothing, and says so rather than failing
obscurely. The **intervention ladder** now bites too: an agent escalated twice is
dropped to `suggest` even if it was hired at `full`, and the refusal says so.

### The runner

The seventh role, and the one that spends real compute. It runs exactly the
workflow it was handed, never edits it to make it work, and refuses a plan with
unbound parameters instead of burning a queue slot on a run that cannot succeed.

Nothing is executed on the planner's word alone. The chain is
**plan → curate → run**: the PI sends the finished plan to the curator, and only a
pass reaches the runner. A block is recorded on the blackboard under
`mission/held` with the problems that caused it.

To let the lab work unattended while you watch what it *would* do:

```bash
bioharness attend --dry-run       # every step resolved, nothing executed
```

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
pytest -q     # 188 passed
```

The suite builds its capabilities out of `sh`, `tr` and `echo`, so it proves the
harness mechanics — binding, DAG ordering, scatter, retries, provenance hashing,
budgets, breakers, mailbox routing, and the dashboard API — on any machine, with no bioinformatics tool
installed.

## Status

Working core and dashboard. Not yet built: HPC submission (Slurm/SGE executor
backends) and encrypted cross-machine transport for the mailbox layer.

MIT.

### Pixelated 3D researchers

The lab now uses original articulated block characters rendered from 3D cuboids,
with per-name colours, lab coats, goggles, and walking poses. No CDN or GPU setup
is required. Select a researcher on the floor or team strip to inspect their
larger portrait; click a floor destination to send them walking through the lab.
Use **3D characters** to switch to the original sprites, **Labels** to simplify
the view, and **Pause motion** to freeze local movement without pausing agents.
Reduced-motion preferences start the view paused. The dashboard also adapts to
narrow screens and keeps failed message drafts available for retry.

Run the frontend regressions with `node --test tests/ui_regression.mjs`, alongside
`python -m pytest` and `node tests/route_check.mjs` from the project root.

### Full 3D voxel laboratory

The **3D Lab** view now renders the entire seven-room laboratory with actual 3D
walls, tiled and wooden floors, windows, doors, equipment, furniture, plants,
lights, shadows, and animated researchers. The existing live roster, agent
selection, and doorway routing remain connected to the scene.

- Drag to orbit; scroll or pinch to zoom; right-drag or two-finger drag to pan.
- Use **Overview** and **Top view** for camera presets. **Follow agent** swoops down
  behind the selected researcher and rides along as they walk.
- The perspective scene floats in a sky with drifting clouds; rooms have their own
  light, screens glow, equipment LEDs blink, and dust drifts through the air.
- **Hide walls** exposes equipment; **Labels** controls room and researcher tags.
- Select a researcher, then click a floor destination to move them locally.
- **Pause motion** stops local walking animation without stopping backend agents.
- Keyboard: focus the scene, then `W` `A` `S` `D` (or arrows) to walk, `Q`/`E` to turn,
  `F` for fullscreen, `Home` to reset, `+`/`-` to zoom, `Escape` to clear selection.
- Turn **3D Lab** off for the original 2D floor. Unsupported WebGL browsers fall
  back automatically. No internet connection or frontend build is needed at runtime.

The lab is called **Voxel Lab** until you name it. Click the name above the scene
to rename it, or set it at launch:

```sh
bioharness ui --lab-name "Kim Lab"
```

The name is saved in the hive (`lab.json`), shown in the 3D view and the browser
tab, and given to the agents, so they know which lab they work in.

The generated asset pack is in `bioharness/ui/assets/voxel/`: 22 portable `.glb`
models, 15 pixel textures, and a model catalog. Rebuild it with
`python tools/generate_voxel_assets.py`. Three.js is bundled with its MIT license.

### Small local-model lab

For a small local team, install Ollama, start its server, and provision these
researchers once in a new workspace:

```sh
ollama pull qwen2.5:3b
bioharness staff hire local-qwen --role planner --provider ollama --model qwen2.5:3b --autonomy read_only
bioharness staff hire local-qc --role validator --provider ollama --model qwen2.5:3b --autonomy read_only
```

Start the dashboard and process only their messages with:

```sh
python -m bioharness.cli ui --attend --only local-qwen local-qc --no-social --no-brief --no-open --port 8876
```

`--only` also works with `bioharness attend`. Other agents stay enrolled but
their pending messages are not processed by this worker. In the lab composer,
select either local researcher and send a `peer.ask` question or a directive.
These test researchers have read-only execution authority. Ollama must be running;
`ollama list` should include `qwen2.5:3b`.
