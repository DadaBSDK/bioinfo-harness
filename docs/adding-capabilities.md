# Adding a capability

The registry is the only integration point. A manifest is data — you never
subclass anything to add a tool.

## Minimal manifest

```yaml
capabilities:
  - id: my_tool           # unique; duplicates are rejected at load
    domain: ngs           # ngs | structural | singlecell | cheminfo | general
    version: "1.0"
    summary: One line, shown in `bioharness caps`
    runtime: {kind: binary, binary: my_tool}
    command: "my_tool --in {{ inputs.data }} --out {{ outdir }}/result.txt"
    inputs:  [{name: data, type: table}]
    outputs: [{name: result, type: table, glob: "result.txt"}]
```

## Template variables

| Token | Value |
|---|---|
| `{{ inputs.<port> }}` | Bound input; a list is joined and shell-quoted |
| `{{ params.<name> }}` | Merged manifest defaults + workflow + `--set` |
| `{{ outdir }}` | The step's private working directory |
| `{{ threads }}` | `params.threads`, else `resources.cpu` |
| `{{ run_id }}`, `{{ step }}` | Run and step identifiers |

A string that is *exactly* one token keeps its native type, so
`{{ params.threads }}` stays an integer rather than becoming `"8"`.

## Ports and the type graph

`type:` is what makes a tool routable. `bioharness plan fastq vcf` works because
`bwa_mem` consumes `fastq` and emits `bam`, and `bcftools_call` consumes `bam`
and emits `vcf`. Reuse existing type names where you can:

`fastq` `bam` `sam` `cram` `vcf` `fasta` `bai` `annotation` · `pdb` `topology`
`coordinates` `trajectory` `energy` `pose` · `table` `metrics` `html` `dir`
`archive`

Port flags: `multiple` (list-valued), `optional` (never blocks a run),
`default`, `glob` (how outputs are collected from `outdir`).

## Runtimes

| kind | Executed as | Probe checks |
|---|---|---|
| `binary` | directly | `binary` on `PATH` |
| `conda` | `<mgr> run -n <env> …` | env exists in `conda env list` |
| `docker` | `docker run --rm -v … -w <outdir>` | `docker` on `PATH` |
| `apptainer` | `apptainer exec --bind … --pwd` | `apptainer` on `PATH` |
| `python` | in-process call | entrypoint module **and `requires:`** import |
| `agent` | delegate to an LLM CLI | provider CLI on `PATH` |

GPU is requested with `resources: {gpu: 1}` — `--gpus all` for Docker, `--nv`
for Apptainer. Input parent directories are bind-mounted automatically.

## Python capabilities

```yaml
  - id: my_analysis
    runtime: {kind: python, entrypoint: "mypkg.analysis:run"}
    requires: [scipy]        # lazily-imported deps, so `doctor` is honest
    command: ""
```

```python
def run(inputs: dict, params: dict, workdir: Path) -> dict[str, str]:
    """Return {output_port_name: path}."""
```

## Checklist

1. `bioharness doctor` — is it detected and available?
2. `bioharness caps <id> -v` — do the ports read correctly?
3. `bioharness plan <in_type> <out_type>` — does it route?
4. `bioharness run <wf> --dry-run` — is the resolved command right?
5. Add `citation:` — the scribe writes it into Methods.
