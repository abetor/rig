"""Tests for core data types, unit ID validation, green-state evaluation, and plan serialization."""






from __future__ import annotations

import re

import pytest

from rig.core.types import VALID_UNIT_ID, Plan, StepResult, Unit, is_green, plan_to_dict



@pytest.mark.parametrize("ok_id", ["u1", "U_1", "abc123", "_x", "a.b-c", "x", "A0._-"])
def test_valid_unit_id_accepts_safe(ok_id):

    assert VALID_UNIT_ID.match(ok_id), f"{ok_id!r} must to lie"


@pytest.mark.parametrize(
    "bad_id",
    [
        "../..",
        "../../src",
        "..",
        ".",
        "a/b",
        "a/../b",
        "/abs/path",
        "/tmp/x",
        "",
        "-lead",
        ".hidden",
        " u1",
        "u1 ",
        "a b",
    ],
)
def test_valid_unit_id_rejects_unsafe(bad_id):

    assert VALID_UNIT_ID.match(bad_id) is None, f"{bad_id!r} must to defend oneself"


def test_valid_unit_id_anchor_is_capital_z_not_dollar():


    assert VALID_UNIT_ID.match("u1\n") is None
    assert VALID_UNIT_ID.match("u1\nrm -rf /") is None

    with_dollar = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
    assert with_dollar.match("u1\n") is not None
    assert with_dollar.match("u1\n").group() == "u1"



_T, _F = StepResult(ok=True), StepResult(ok=False)


@pytest.mark.parametrize(
    "d, v, g, expected",
    [
        (_T, _T, _T, True),
        (_F, _T, _T, False),
        (_T, _F, _T, False),
        (_T, _T, _F, False),
        (_T, None, _T, False),
        (_T, _T, None, False),
        (_T, None, None, False),
        (_F, None, None, False),
    ],
)
def test_is_green_truth_table(d, v, g, expected):

    assert is_green(d, v, g) is expected


def test_is_green_returns_strict_bool():

    assert is_green(_T, _T, _T) is True
    assert is_green(_T, None, _T) is False



def _dict_to_plan(d: dict) -> Plan:

    return Plan(
        goal=d["goal"],
        units=[Unit(u["id"], u["instruction"], u.get("kind", "writer")) for u in d["units"]],
        gate_cmd=list(d["gate_cmd"]),
        context=d.get("context", ""),
    )


def test_plan_to_dict_roundtrip_idempotent():

    obj = {
        "goal": "g",
        "units": [{"id": "u1", "instruction": "do", "kind": "readonly"}],
        "gate_cmd": ["run-gate", "-q"],
    }
    assert plan_to_dict(_dict_to_plan(obj)) == obj


def test_plan_to_dict_keeps_context():

    p = Plan(goal="g", units=[Unit("u1", "a")], gate_cmd=["run-gate"], context="ctx")
    d = plan_to_dict(p)
    assert d["context"] == "ctx" and _dict_to_plan(d).context == "ctx"


def test_plan_to_dict_omits_empty_context():

    p = Plan(goal="g", units=[Unit("u1", "a")], gate_cmd=["run-gate"])
    assert "context" not in plan_to_dict(p)


def test_plan_to_dict_kind_defaults_writer():

    p = Plan(goal="g", units=[Unit("u1", "a")], gate_cmd=["run-gate"])
    assert plan_to_dict(p)["units"][0]["kind"] == "writer"


def test_plan_to_dict_gate_cmd_is_copy_not_alias():

    p = Plan(goal="g", units=[Unit("u1", "a")], gate_cmd=["run-gate", "-q"])
    d = plan_to_dict(p)
    d["gate_cmd"].append("--mutated")
    assert p.gate_cmd == ["run-gate", "-q"]
