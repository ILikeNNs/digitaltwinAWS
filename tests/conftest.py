import importlib
import os
import sys
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "digitaltwin"
# The package's modules import each other (relatively); dropped from sys.modules so every test loads them fresh
PROJECT_MODULES = ("helpers", "database", "rag_engine", "app")


@pytest.fixture
def load_module(monkeypatch):
    """Loads a digitaltwin module fresh, optionally replacing some of its imports with stubs.

    `stubs` maps module names to either a ready-made object or a dict of attributes for a stand-in module,
    e.g. {"openai": {"OpenAI": MagicMock()}}. Names of the package's own modules ("database", "rag_engine", ...)
    stub the package member (digitaltwin.database, ...), which is what the relative imports resolve to.
    The `project` argument names the project the test file targets and is kept for compatibility with the shared
    test layout; this repository holds a single project.
    """
    monkeypatch.syspath_prepend(PROJECT_ROOT)

    def _load(project, name, stubs=None):
        stubs = stubs or {}
        for mod_name, stub in stubs.items():
            if isinstance(stub, dict):
                module = types.ModuleType(mod_name)
                module.__dict__.update(stub)
                stub = module
            key = f"{PACKAGE}.{mod_name}" if mod_name in PROJECT_MODULES else mod_name
            monkeypatch.setitem(sys.modules, key, stub)
        for mod_name in PROJECT_MODULES:
            if mod_name not in stubs:
                monkeypatch.delitem(sys.modules, f"{PACKAGE}.{mod_name}", raising=False)
        return importlib.import_module(f"{PACKAGE}.{name}")

    return _load
