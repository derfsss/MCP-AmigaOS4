"""SequenceRunner tests — pure orchestration logic."""

from __future__ import annotations

import pytest

from amiga_fleet_mcp.installer.sequences import (
    Sequence,
    SequenceRunner,
    Step,
)


def _seq(*steps_with_fns):
    return Sequence(
        machine="TEST", description="test sequence",
        steps=[
            Step(name=name, doc=name, fn=fn)
            for name, fn in steps_with_fns
        ],
    )


@pytest.mark.asyncio
async def test_dry_run_returns_planned_without_executing():
    fired: list[str] = []

    async def s(_ctx):
        fired.append("s")
        return None

    seq = _seq(("s1", s), ("s2", s))
    res = await SequenceRunner(sequence=seq).run(dry_run=True)
    assert fired == []
    assert res.overall == "planned"
    assert res.dry_run is True
    assert [s.name for s in res.steps] == ["s1", "s2"]
    assert all(s.status == "planned" for s in res.steps)


@pytest.mark.asyncio
async def test_run_executes_in_order():
    fired: list[str] = []

    async def make(name):
        async def fn(_ctx):
            fired.append(name)
            return name
        return fn

    seq = _seq(
        ("a", await make("a")),
        ("b", await make("b")),
        ("c", await make("c")),
    )
    res = await SequenceRunner(sequence=seq).run(dry_run=False)
    assert fired == ["a", "b", "c"]
    assert res.overall == "ok"
    assert res.failed_step is None
    assert all(s.status == "ok" for s in res.steps)
    assert [s.output for s in res.steps] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_halts_on_first_failure():
    fired: list[str] = []

    async def ok(_ctx):
        fired.append("ok")

    async def boom(_ctx):
        fired.append("boom")
        raise RuntimeError("step failed")

    async def never(_ctx):
        fired.append("never")  # should not run

    seq = _seq(("ok", ok), ("boom", boom), ("never", never))
    res = await SequenceRunner(sequence=seq).run(dry_run=False)
    assert fired == ["ok", "boom"]  # never not invoked
    assert res.overall == "fail"
    assert res.failed_step == "boom"
    assert res.steps[0].status == "ok"
    assert res.steps[1].status == "fail"
    assert "step failed" in (res.steps[1].error or "")
    assert len(res.steps) == 2  # third step never recorded


@pytest.mark.asyncio
async def test_ctx_threaded_through_steps():
    async def s1(ctx):
        ctx["mounted"] = True
        return "s1 set"

    async def s2(ctx):
        if not ctx.get("mounted"):
            raise RuntimeError("ctx not propagated")
        return "s2 saw mounted"

    seq = _seq(("s1", s1), ("s2", s2))
    res = await SequenceRunner(sequence=seq, initial_ctx={}).run(dry_run=False)
    assert res.overall == "ok"
    assert res.steps[1].output == "s2 saw mounted"


@pytest.mark.asyncio
async def test_initial_ctx_visible_to_first_step():
    async def s(ctx):
        assert ctx["target"] == "x5000"
        assert ctx["dest_volume"] == "BootTest:"
        return "got ctx"

    seq = _seq(("only", s))
    res = await SequenceRunner(
        sequence=seq,
        initial_ctx={"target": "x5000", "dest_volume": "BootTest:"},
    ).run(dry_run=False)
    assert res.overall == "ok"
