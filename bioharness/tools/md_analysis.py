"""Trajectory analysis as an in-process capability.

Uses MDAnalysis when it is installed; the capability probe reports it missing
otherwise, so the harness blocks the step cleanly instead of failing mid-run.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def rmsd(inputs: dict[str, Any], params: dict[str, Any],
         workdir: Path) -> dict[str, str]:
    """Backbone RMSD to frame 0, plus radius of gyration, per frame."""
    import MDAnalysis as mda
    from MDAnalysis.analysis import rms

    topology = inputs.get("topology")
    trajectory = inputs["trajectory"]
    universe = (mda.Universe(str(topology), str(trajectory)) if topology
                else mda.Universe(str(trajectory)))

    selection = params.get("selection", "backbone")
    stride = int(params.get("stride", 1))

    analysis = rms.RMSD(universe, select=selection).run(step=stride)
    selected = universe.select_atoms(selection)

    out = workdir / "rmsd.csv"
    with open(out, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["frame", "time_ps", "rmsd_angstrom", "rmsd_nm", "rgyr_angstrom"])
        for (frame, time_ps, value), ts in zip(analysis.results.rmsd,
                                               universe.trajectory[::stride]):
            writer.writerow([int(frame), round(float(time_ps), 3),
                             round(float(value), 4), round(float(value) / 10.0, 5),
                             round(float(selected.radius_of_gyration()), 4)])
    return {"metrics": str(out)}
