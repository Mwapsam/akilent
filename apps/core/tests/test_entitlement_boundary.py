"""One gate for commercial access: application code asks ``apps.billing.api`` and nothing else.

Code outside billing must never decide what a business can use by reading an old Plan capability
column or ``ModuleSubscription``. If it could, changing a plan in the Operator Console would stop
matching what the app does, and pricing would need a deploy.

The check parses source with ``ast``, so comments, docstrings and strings don't count, and a
different variable name doesn't get around it (``x.bulk_email`` is caught whatever ``x`` is).
"""
import ast
from pathlib import Path

APPS = Path(__file__).resolve().parents[2]

# Old Plan capability columns (apps.billing.features.LEGACY_FLAGS plus the unenforced one).
PLAN_FLAGS = {
    "email_apis", "email_templates", "bulk_email", "tracking_webhooks", "outbound_webhooks",
    "detailed_analytics", "has_priority_support", "inbound_email",
}
REMOVED_API = {"module_enabled", "has_feature", "set_module_enabled", "enable_module"}

# (path relative to apps/, attribute) pairs that share a flag's name but aren't a plan column.
# Each needs a reason; keep this list short and reviewed.
ALLOWED = {
    # LimitChecker(account).has_feature("...") is the billing shim over entitled(); calling a
    # method named has_feature on a LimitChecker is fine, only billing_api.has_feature is gone.
}


def _python_files():
    for path in APPS.rglob("*.py"):
        rel = path.relative_to(APPS)
        parts = rel.parts
        if parts[0] == "billing" or "migrations" in parts or "tests" in parts:
            continue
        yield path, rel.as_posix()


def _violations():
    found = []
    for path, rel in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in PLAN_FLAGS and (rel, node.attr) not in ALLOWED:
                found.append(f"{rel}:{node.lineno} reads .{node.attr}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" \
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) \
                    and node.args[1].value in PLAN_FLAGS:
                found.append(f"{rel}:{node.lineno} getattr(..., {node.args[1].value!r})")
            elif isinstance(node, ast.Name) and node.id == "ModuleSubscription":
                found.append(f"{rel}:{node.lineno} uses ModuleSubscription")
            elif isinstance(node, ast.alias) and node.name.split(".")[-1] == "ModuleSubscription":
                found.append(f"{rel}: imports ModuleSubscription")
            elif isinstance(node, ast.alias) and node.name in REMOVED_API:
                found.append(f"{rel}: imports billing's removed {node.name}")
            elif isinstance(node, ast.Attribute) and node.attr in REMOVED_API - {"has_feature"}:
                found.append(f"{rel}:{node.lineno} calls .{node.attr}")
            elif isinstance(node, ast.Attribute) and node.attr == "has_feature" \
                    and isinstance(node.value, ast.Name) and node.value.id in ("billing_api", "api"):
                found.append(f"{rel}:{node.lineno} calls {node.value.id}.has_feature")
    return found


def test_only_the_billing_api_decides_commercial_access():
    violations = _violations()
    assert not violations, (
        "Ask apps.billing.api (entitled / usable / access) instead:\n  " + "\n  ".join(violations))


def test_the_check_catches_aliases_and_other_variable_names(tmp_path, monkeypatch):
    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "bad.py").write_text(
        "from apps.billing.models import ModuleSubscription as MS\n"
        "def f(anything):\n"
        "    return anything.plan.bulk_email or getattr(anything, 'email_apis')\n"
        "\"\"\"a docstring mentioning plan.bulk_email is fine\"\"\"\n",
        encoding="utf-8")
    monkeypatch.setattr(__import__(__name__, fromlist=["APPS"]), "APPS", tmp_path)
    found = _violations()
    assert any("imports ModuleSubscription" in v for v in found)
    assert any(".bulk_email" in v for v in found)
    assert any("'email_apis'" in v for v in found)
    assert len([v for v in found if "bulk_email" in v]) == 1, "the docstring must not count"
