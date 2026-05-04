"""Generic step-runner for install sequences.

A `Step` is an awaitable callable + a name + a doc string. A `Sequence`
is a list of Steps. The `SequenceRunner` runs them in order, captures
per-step duration / output / errors, and halts on the first failure
(no implicit rollback — each step is responsible for being either
idempotent or reversible by re-running).

Each step receives a `ctx` dict that's threaded through the run, so
later steps can read state (e.g. mount detection) written by earlier
ones.

`dry_run=True` returns the planned step list without executing — used
by callers (and the MCP `installer.install_x5000(dry_run=True)` tool)
to preview the pipeline before committing.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

StepFn = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass
class Step:
    """A single named install step.

    `fn(ctx)` does the work and returns whatever value should be
    associated with the step (often None or a small dict). Raising any
    exception aborts the sequence.
    """
    name: str
    doc: str
    fn: StepFn


@dataclass
class Sequence:
    """An ordered list of Steps for a particular machine."""
    machine: str
    description: str
    steps: list[Step]


class StepResult(BaseModel):
    name: str
    doc: str
    status: str  # "ok" / "fail" / "skipped" / "planned"
    duration_s: float = 0.0
    output: Any = None
    error: str | None = None


class SequenceResult(BaseModel):
    machine: str
    description: str
    overall: str  # "ok" / "fail" / "planned"
    total_duration_s: float
    steps: list[StepResult]
    failed_step: str | None = None
    dry_run: bool


@dataclass
class SequenceRunner:
    sequence: Sequence
    initial_ctx: dict[str, Any] = field(default_factory=dict)

    async def run(self, *, dry_run: bool = False) -> SequenceResult:
        if dry_run:
            return SequenceResult(
                machine=self.sequence.machine,
                description=self.sequence.description,
                overall="planned",
                total_duration_s=0.0,
                dry_run=True,
                steps=[
                    StepResult(name=s.name, doc=s.doc, status="planned")
                    for s in self.sequence.steps
                ],
            )

        ctx = dict(self.initial_ctx)
        results: list[StepResult] = []
        t_total = time.monotonic()
        failed: str | None = None

        for step in self.sequence.steps:
            t0 = time.monotonic()
            try:
                output = await step.fn(ctx)
                results.append(StepResult(
                    name=step.name, doc=step.doc, status="ok",
                    duration_s=time.monotonic() - t0,
                    output=output,
                ))
            except Exception as e:
                results.append(StepResult(
                    name=step.name, doc=step.doc, status="fail",
                    duration_s=time.monotonic() - t0,
                    error=f"{type(e).__name__}: {e}",
                    output=traceback.format_exc()[-500:],
                ))
                failed = step.name
                break

        return SequenceResult(
            machine=self.sequence.machine,
            description=self.sequence.description,
            overall="fail" if failed else "ok",
            total_duration_s=time.monotonic() - t_total,
            steps=results,
            failed_step=failed,
            dry_run=False,
        )
