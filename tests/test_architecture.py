"""The safety boundary is structural, so test the structure."""
import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parent.parent / "smartdialer"
FORBIDDEN = ("safety", "allocator", "providers", "store", "db", "calls", "worker", "housekeeping", "agents")


def imports(path):
    tree = ast.parse(path.read_text())
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            out.append(("." * n.level) + (n.module or "") + ":" + ",".join(a.name for a in n.names))
        elif isinstance(n, ast.Import):
            out += [a.name for a in n.names]
    return out


def test_pacing_cannot_reach_safety_database_or_providers():
    for f in (PKG / "pacing").glob("*.py"):
        for imp in imports(f):
            mod = imp.split(":")[0].lstrip(".")
            names = imp.split(":")[1] if ":" in imp else ""
            for bad in FORBIDDEN:
                assert not (mod == bad or mod.startswith(bad + ".") or (mod == "" and bad in names.split(","))), \
                    f"{f.name} imports {imp}"
            assert "SafetyConfig" not in imp, f"{f.name} must never see the safety limits"


def test_only_the_allocator_and_worker_hold_a_provider_router():
    holders = []
    for f in PKG.rglob("*.py"):
        if "ProviderRouter" in f.read_text() and f.parent.name != "sim":
            holders.append(f.name)
    assert sorted(set(holders)) <= ["allocator.py", "housekeeping.py", "router.py", "worker.py"]
    assert "predictive.py" not in holders and "progressive.py" not in holders and "safety.py" not in holders


def test_safety_controller_does_not_import_provider_code():
    for imp in imports(PKG / "safety.py"):
        assert "providers" not in imp, imp
