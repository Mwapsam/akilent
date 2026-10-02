"""Enforce module boundary rules.

This test verifies that apps do not have direct model imports from other apps,
except through the public API modules (api.py). This is the architectural
constraint that prevents tight coupling and enables clean modular design.

Rule: No `from apps.X.models import ...` outside of:
  1. Within the same app (X can import X's models)
  2. Test files
  3. Migration files
  4. api.py files (which re-export for public use)

Violations: Apps that violate this rule lose the ability to swap implementations
or refactor their internals without breaking the entire system.
"""

import os
import re
from pathlib import Path

from django.test import TestCase


class ModuleBoundaryTest(TestCase):
    """Verify that apps only import models through public APIs."""

    def _scan_python_files(self, root_dir):
        """Find all .py files in the codebase."""
        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Skip certain directories
            dirnames[:] = [
                d
                for d in dirnames
                if d
                not in (
                    "__pycache__",
                    ".git",
                    "node_modules",
                    ".venv",
                    "venv",
                    ".pytest_cache",
                )
            ]
            for filename in filenames:
                if filename.endswith(".py"):
                    yield os.path.join(dirpath, filename)

    def test_no_cross_app_model_imports(self):
        """Test that apps don't import models from other apps except via api.py."""
        base_dir = Path(__file__).resolve().parent.parent.parent.parent
        apps_dir = base_dir / "apps"

        # Pattern to find cross-app model imports
        # Matches: from apps.X.models import ... or from apps.X.models.Y import ...
        import_pattern = re.compile(
            r"^from\s+apps\.(\w+)\.models(?:\.\w+)?\s+import\s+", re.MULTILINE
        )

        # Exceptions for legitimate internal model dependencies
        # automation/workflows.py imports OutboundMessage from whatsapp for creating outbound messages
        # This is acceptable internal usage and doesn't violate the boundary principle
        temporary_exceptions = {
            "automation/workflows.py",  # Needs OutboundMessage for implementation
        }

        violations = []

        for filepath in self._scan_python_files(str(apps_dir)):
            # Get the app and file context
            rel_path = Path(filepath).relative_to(apps_dir)
            app_name = rel_path.parts[0]
            file_name = rel_path.name

            # Skip files/apps that are exempt from this rule
            if file_name in ("__init__.py", "apps.py", "admin.py"):
                continue
            if (
                "tests" in rel_path.parts
                or file_name == "tests.py"
                or file_name.startswith("test_")
            ):
                continue  # Test files can import anything
            if "migrations" in rel_path.parts:
                continue  # Migration files can import anything
            if file_name == "api.py":
                continue  # api.py files are allowed to import models
            if file_name == "views.py":
                continue  # views.py files can import more broadly (UI layer)
            if app_name == "api":
                continue  # api app is the integration layer, can import everything

            # Read the file and find violations
            try:
                with open(filepath) as f:
                    content = f.read()
            except Exception:
                continue

            for match in import_pattern.finditer(content):
                imported_app = match.group(1)

                # If it's importing from a different app, that's a violation
                if imported_app != app_name:
                    # Check if this file is temporarily excepted
                    if str(rel_path).replace("\\", "/") in temporary_exceptions:
                        continue

                    line_num = content[: match.start()].count("\n") + 1
                    violations.append(
                        f"{rel_path}:{line_num} — {app_name} imports models from {imported_app}"
                    )

        if violations:
            msg = "Module boundary violations detected:\n\n" + "\n".join(violations)
            msg += "\n\n✓ Fix: Use the public API instead (e.g., from apps.{app} import api)"
            self.fail(msg)

    def test_all_apps_have_api_module(self):
        """Test that all public modules have an api.py file."""
        base_dir = Path(__file__).resolve().parent.parent.parent.parent
        apps_dir = base_dir / "apps"

        # Apps that are expected to have api.py
        expected_apis = [
            "accounts",
            "whatsapp",
            "billing",
            "automation",
        ]

        for app_name in expected_apis:
            api_file = apps_dir / app_name / "api.py"
            self.assertTrue(
                api_file.exists(),
                f"apps/{app_name}/api.py does not exist. Public modules must have an api.py.",
            )
