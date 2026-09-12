"""Load the plugin directory as the package Hermes would create (``hermes_plugins.metamask_wallet``)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PKG = "hermes_plugins.metamask_wallet"


def _load_package():
    if PKG in sys.modules:
        return sys.modules[PKG]
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        PKG, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plugin():
    return _load_package()


@pytest.fixture
def mm(plugin):
    return sys.modules[f"{PKG}.mm_client"]


@pytest.fixture
def tools(plugin):
    return sys.modules[f"{PKG}.tools"]


class FakeCtx:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.tools = {}
        self.hooks = {}
        self.commands = {}
        self.skills = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, name, toolset, schema, handler, check_fn=None, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "check_fn": check_fn}

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)

    def register_command(self, name, handler, description="", **kw):
        self.commands[name] = handler

    def register_skill(self, name, path, description="", **kw):
        self.skills[name] = path


@pytest.fixture
def fake_ctx():
    return FakeCtx()
