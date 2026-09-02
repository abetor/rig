"""Architecture boundary tests for imports, constants, roles, and transport layering."""












from __future__ import annotations

import ast
import pathlib

import rig

RIG = pathlib.Path(rig.__file__).resolve().parent
FORBIDDEN_FOR_LOW = ("rig.adapters", "rig.flow", "rig.tools", "rig.entry")


def _imports(path: pathlib.Path) -> set:


    pkg_parts = list(path.relative_to(RIG.parent).parts[:-1])
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                out.add(node.module or "")
            else:
                base = pkg_parts[:len(pkg_parts) - (node.level - 1)]
                out.add(".".join(base + ([node.module] if node.module else [])))
    return out


def test_core_and_checks_do_not_import_upper_layers():


    for pkg in ("core", "checks"):
        for f in sorted((RIG / pkg).glob("*.py")):
            bad = sorted(m for m in _imports(f)
                         if m.startswith(FORBIDDEN_FOR_LOW))
            assert not bad, f"{pkg}/{f.name} imports upper layer: {bad}"


def test_constants_have_single_source():


    from rig.adapters import base
    from rig.core import types
    from rig.db import convention
    from rig.flow import phase
    assert phase.PERMS is types.VALID_PERMS
    assert base.VALID_PERMS is types.VALID_PERMS
    assert base.ENGINE_DIR == types.ENGINE_DIR == convention.DB_DIRNAME == ".rig"


def test_flow_runner_is_generic():


    runner_py = RIG / "flow" / "runner.py"
    in_rig = {m for m in _imports(runner_py) if m.startswith("rig.")}
    assert in_rig <= {"rig.flow.phase"}, f"runner import lichen: {sorted(in_rig)}"
    roles = {"scout", "repro", "plan", "critic", "checkpoint", "converse", "unit", "units",
             "review", "gate", "reconcile", "notify", "orchestrator", "worker", "judge"}
    lits = {n.value for n in ast.walk(ast.parse(runner_py.read_text(encoding="utf-8")))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not (lits & roles), f"runner carries role-literaly: {sorted(lits & roles)}"


def test_transport_does_not_import_engine():

    for f in sorted((RIG / "transport").glob("*.py")):
        bad = sorted(m for m in _imports(f) if m.startswith("rig.") and not m.startswith("rig.transport"))
        assert not bad, f"transport/{f.name} imports engine: {bad}"
