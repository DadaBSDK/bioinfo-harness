"""Data download capabilities (ENA runs, reference files).

Implemented in Python rather than shell templates so failures (unknown
accession, 404, truncated gzip) produce clear error messages and paths with
spaces are handled safely.

Standard library only: urllib and gzip.
"""
from __future__ import annotations

import gzip
import io
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENA_REPORT = ("https://www.ebi.ac.uk/ena/portal/api/filereport"
              "?accession={acc}&result=read_run&fields=fastq_ftp&format=tsv")
TIMEOUT = 120
AGENT = {"User-Agent": "bioharness/0.1 (+https://example.invalid)"}


class FetchError(RuntimeError):
    """Something the operator can fix: a bad accession, a dead URL, an empty file."""


def _open(url: str):
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=AGENT),
                                      timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url} returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"cannot reach {url}: {exc}") from None


def _download(url: str, dest: Path, max_lines: int = 0,
              fastq: bool = False) -> int:
    """Stream a (possibly gzipped) file to `dest`, optionally truncated to whole
    FASTQ records.
    """
    written = 0
    try:
        with _open(url) as response:
            stream: Any = response
            if url.endswith(".gz"):
                stream = gzip.GzipFile(fileobj=io.BufferedReader(response))
            with open(dest, "wb") as out:
                for line in stream:
                    out.write(line)
                    written += 1
                    if max_lines and written >= max_lines:
                        break
    except EOFError:
        # Expected when the stream is cut at max_lines; the size check below covers
        # an empty download.
        pass
    except (urllib.error.URLError, OSError) as exc:
        if not written:
            raise FetchError(f"{url} died after {written} lines: {exc}") from None
    if not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise FetchError(f"{url} gave nothing back")
    if fastq and written % 4:
        # Drop a trailing partial FASTQ record.
        keep = written - (written % 4)
        lines = dest.read_bytes().split(b"\n")
        dest.write_bytes(b"\n".join(lines[:keep]) + b"\n")
        written = keep
    return written


def ena(inputs: dict[str, Any], params: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Download the FASTQ files ENA holds for a run accession."""
    acc = str(params.get("accession", "")).strip()
    if not acc:
        raise FetchError("no accession given. Supply one, e.g. accession=SRR12345678 "
                         "-- a run accession (SRR/ERR/DRR), not a BioProject.")
    max_reads = int(params.get("max_reads", 0) or 0)

    try:
        with _open(ENA_REPORT.format(acc=acc)) as response:
            report = response.read().decode("utf-8", "replace").splitlines()
    except FetchError as exc:
        # ENA returns 400 for a malformed accession.
        raise FetchError(f"ENA does not recognise {acc!r} as a run accession "
                         f"(it wants SRR/ERR/DRR, not a BioProject or a sample "
                         f"id). ENA said: {str(exc)[-60:]}") from None
    if not report:
        raise FetchError(f"ENA returned an empty report for {acc!r}")
    # Look up columns by header name; column 0 is `run_accession`, not a URL.
    header = report[0].split("\t")
    try:
        column = header.index("fastq_ftp")
    except ValueError:
        raise FetchError(f"ENA report for {acc!r} has no fastq_ftp column "
                         f"(got {', '.join(header)})") from None
    urls: list[str] = []
    for row in report[1:]:
        if not row.strip():
            continue
        cells = row.split("\t")
        if len(cells) <= column:
            continue
        for part in cells[column].split(";"):
            if part.strip():
                urls.append(part.strip())
    if not urls:
        raise FetchError(f"ENA lists no FASTQ for {acc!r}. Check the accession, or "
                         f"the run may be submitted but not yet released.")

    written = []
    for i, url in enumerate(urls, start=1):
        if not url.startswith("http"):
            url = "https://" + url
        dest = workdir / f"{acc}_{i}.fastq"
        lines = _download(url, dest, max_lines=max_reads * 4 if max_reads else 0,
                         fastq=True)
        written.append({"file": dest.name, "reads": lines // 4, "from": url})
    return {"fetched": str(written)}


def reference(inputs: dict[str, Any], params: dict[str, Any],
              workdir: Path) -> dict[str, Any]:
    """Download a reference genome FASTA from a URL, decompressing if needed."""
    url = str(params.get("url", "")).strip()
    if not url:
        raise FetchError("no url given. Supply the FASTA to align against, e.g. "
                         "url=https://ftp.ensemblgenomes.ebi.ac.uk/.../genome.fa.gz")
    name = str(params.get("name") or "reference").replace("/", "_")
    dest = workdir / f"{name}.fa"
    lines = _download(url, dest)
    head = dest.open().readline().strip()
    if not head.startswith(">"):
        raise FetchError(f"{url} does not look like FASTA (first line: {head[:60]!r})")
    return {"reference": str(dest), "lines": str(lines)}
