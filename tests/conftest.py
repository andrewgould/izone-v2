"""Test fixtures.

api.py is deliberately free of Home Assistant imports so the protocol layer
can be tested without installing homeassistant. It is loaded directly from
its file path because importing the izone_v2 package would execute
__init__.py, which does require Home Assistant.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

API_PATH = (
    pathlib.Path(__file__).parent.parent
    / "custom_components"
    / "izone_v2"
    / "api.py"
)

spec = importlib.util.spec_from_file_location("izone_api", API_PATH)
api = importlib.util.module_from_spec(spec)
sys.modules["izone_api"] = api
spec.loader.exec_module(api)
