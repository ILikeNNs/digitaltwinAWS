import importlib.util
import os
import sys
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Project modules that import each other; dropped from sys.modules so every test loads them fresh
PROJECT_MODULES = ("helpers", "database", "rag_engine", "app")


@pytest.fixture
def load_module(monkeypatch):
    """Loads a project module fresh, optionally replacing some of its imports with stubs.

    `stubs` maps module names to either a ready-made object or a dict of attributes for a stand-in module,
    e.g. {"openai": {"OpenAI": MagicMock()}}. The `project` argument names the project the test file targets
    and is kept for compatibility with the shared test layout; this repository holds a single project.
    """
    monkeypatch.syspath_prepend(PROJECT_ROOT)

    def _load(project, name, stubs=None):
        stubs = stubs or {}
        for mod_name, stub in stubs.items():
            if isinstance(stub, dict):
                module = types.ModuleType(mod_name)
                module.__dict__.update(stub)
                stub = module
            monkeypatch.setitem(sys.modules, mod_name, stub)
        for mod_name in PROJECT_MODULES:
            if mod_name not in stubs:
                monkeypatch.delitem(sys.modules, mod_name, raising=False)

        spec = importlib.util.spec_from_file_location(name, os.path.join(PROJECT_ROOT, f"{name}.py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    return _load
