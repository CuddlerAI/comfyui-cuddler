"""Load the top-level package the way ComfyUI does, against stand-ins for ComfyUI's modules.

nodes.load_custom_node (ComfyUI 0.14 to 0.39) names the module after the folder's full path with "." replaced by
"_x_", builds its spec from <folder>/__init__.py alone, and does not put the folder on sys.path. A sibling import
written `from cuddler.x import y` therefore works from a checkout (the repository root is on sys.path there) and
fails inside ComfyUI, so the loader below reproduces all three and hides the repository from sys.path/sys.modules.
"""
from abc import ABC, abstractmethod
import asyncio
import contextlib
import importlib.util
import inspect
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
OUTSIDE_COMFYUI = {"cuddler", "cuddler_host", "cuddler_plugin"}  # names the repository answers to in a checkout


def comfy_stubs(user_dir):
    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(
        routes=web.RouteTableDef(), client_id="offline-test", send_progress_text=mock.Mock()))

    class ComfyExtension(ABC):  # as comfy_api.latest: on_load is optional, get_node_list is abstract
        async def on_load(self):
            return None

        @abstractmethod
        async def get_node_list(self):
            ...

    latest = types.ModuleType("comfy_api.latest")
    latest.ComfyExtension = ComfyExtension

    class Input:
        def __init__(self, id, **kwargs):
            self.id = id
            self.__dict__.update(kwargs)

    class Option:
        def __init__(self, key, inputs):
            self.key, self.inputs = key, inputs

    class Schema:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class NodeOutput:
        def __init__(self, *args):
            self.result = args

        def __getitem__(self, index):
            return self.result[index]

    class ComfyNode:
        hidden = types.SimpleNamespace(unique_id="test-node")

    latest.io = types.SimpleNamespace(
        ComfyNode=ComfyNode, Schema=Schema, NodeOutput=NodeOutput,
        Int=types.SimpleNamespace(Input=Input), Float=types.SimpleNamespace(Input=Input),
        String=types.SimpleNamespace(Input=Input), Combo=types.SimpleNamespace(Input=Input),
        Boolean=types.SimpleNamespace(Input=Input), Image=types.SimpleNamespace(Input=Input),
        Video=types.SimpleNamespace(Output=lambda: "VIDEO"),
        DynamicCombo=types.SimpleNamespace(Input=Input, Option=Option),
        ControlAfterGenerate=types.SimpleNamespace(fixed="fixed"),
        Hidden=types.SimpleNamespace(unique_id="UNIQUE_ID"))
    # Decoding is tested against real PyAV/ComfyUI in tests/host, never by this stand-in.
    latest.InputImpl = types.SimpleNamespace(VideoFromFile=None)
    comfy_api = types.ModuleType("comfy_api")
    comfy_api.latest = latest
    comfy = types.ModuleType("comfy")
    comfy.model_management = types.SimpleNamespace(processing_interrupted=lambda: False)
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_user_directory = lambda: str(user_dir)
    folder_paths.get_system_user_directory = lambda name="system": str(Path(user_dir) / f"__{name}")
    return {"server": server, "comfy_api": comfy_api, "comfy_api.latest": latest,
            "comfy": comfy, "folder_paths": folder_paths}


@contextlib.contextmanager
def comfyui_process(user_dir):
    """ComfyUI's view when it imports a custom node: its own modules on sys.path, not this repository."""
    here = os.path.normcase(str(ROOT))
    outside = [entry for entry in sys.path if os.path.normcase(os.path.abspath(entry or os.curdir)) != here]
    with mock.patch.dict(sys.modules, comfy_stubs(user_dir)), mock.patch.object(sys, "path", outside):
        for key in [key for key in sys.modules if key.partition(".")[0] in OUTSIDE_COMFYUI]:
            del sys.modules[key]
        yield


def load_like_comfyui():
    """nodes.load_custom_node for a folder. Returns the module's name in sys.modules and the module."""
    name = str(ROOT).replace(".", "_x_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "__init__.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return name, module


class EntrypointTest(unittest.TestCase):
    def test_registers_routes_web_directory_and_an_extension(self):
        with tempfile.TemporaryDirectory() as tmp, comfyui_process(tmp):
            name, module = load_like_comfyui()
            runtime = sys.modules[name + ".cuddler_host.runtime"]
            errors = sys.modules[name + ".cuddler.errors"]
            runtime.configure_transport(_OfflineTransport(errors.CuddlerError))
            # ComfyUI takes the NODE_CLASS_MAPPINGS branch first and then never looks for comfy_entrypoint.
            self.assertIsNone(getattr(module, "NODE_CLASS_MAPPINGS", None))
            self.assertEqual(module.WEB_DIRECTORY, "./web")
            self.assertEqual(os.path.abspath(os.path.join(ROOT, module.WEB_DIRECTORY)), str(ROOT / "web"))
            routes = sys.modules["server"].PromptServer.instance.routes
            self.assertEqual({(route.method, route.path) for route in routes},
                             {("GET", "/cuddler/status"), ("POST", "/cuddler/key"), ("DELETE", "/cuddler/key"),
                              ("POST", "/cuddler/estimate")})
            self.assertTrue(inspect.iscoroutinefunction(module.comfy_entrypoint))
            extension = asyncio.run(module.comfy_entrypoint())
            self.assertIsInstance(extension, sys.modules["comfy_api.latest"].ComfyExtension)
            nodes = asyncio.run(extension.get_node_list())
            self.assertEqual([node.__name__ for node in nodes], ["CuddlerVideo"])
            with self.assertLogs("cuddler", level="WARNING") as logs:
                asyncio.run(extension.on_load())
                for thread in threading.enumerate():
                    if thread.name == "cuddler-refresh":
                        thread.join(5)
            self.assertIn("network_error", logs.output[0])
            self.assertTrue((Path(tmp) / "__cuddler").is_dir())

    def test_on_load_reports_any_start_up_failure_instead_of_failing(self):
        """ComfyUI drops the whole extension, its nodes with it, when on_load raises."""
        with tempfile.TemporaryDirectory() as tmp, comfyui_process(tmp):
            name, module = load_like_comfyui()
            errors = sys.modules[name + ".cuddler.errors"]
            extension = asyncio.run(module.comfy_entrypoint())
            for failure, reported in ((errors.CuddlerError("state_unavailable", status=500), "state_unavailable"),
                                      (ValueError("invalid system user name"), "ValueError"),
                                      (RuntimeError("can't start new thread"), "RuntimeError"),
                                      (sqlite3.DatabaseError("file is not a database"), "DatabaseError")):
                broken = mock.Mock(side_effect=failure)
                with self.subTest(failure=type(failure).__name__), \
                        mock.patch.object(sys.modules[name + ".cuddler_host.extension"], "get_runtime", broken), \
                        self.assertLogs("cuddler", level="WARNING") as logs:
                    asyncio.run(extension.on_load())  # reported when a node runs, not by failing to load
                    self.assertIn(reported, logs.output[0])


class _OfflineTransport:
    """No network in unit tests: every call fails the way an unreachable Platform does."""

    def __init__(self, error_class):
        self._error_class = error_class

    def send(self, request):
        raise self._error_class("network_error")


if __name__ == "__main__":
    unittest.main()
