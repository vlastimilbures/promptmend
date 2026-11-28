"""Imports scripts/wordings.py, which is a script module rather than a package module (#414).

Registered as ``wordings`` in sys.modules, so the scripts that import it by that name
(render_prompt_template.py, bench_models.py) load in the tests once this module is imported.
"""

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

__all__ = ["DOMAIN", "Wordings", "default_wordings", "parse"]

if TYPE_CHECKING:
    # mypy checks scripts/ as top-level modules, so the tests see the script's real types.
    from wordings import DOMAIN, Wordings, default_wordings, parse
else:
    _SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "wordings.py"
    _spec = importlib.util.spec_from_file_location("wordings", _SCRIPT)
    assert _spec
    assert _spec.loader
    _module = importlib.util.module_from_spec(_spec)
    # @dataclass resolves annotations through sys.modules, so register before executing.
    sys.modules["wordings"] = _module
    _spec.loader.exec_module(_module)
    DOMAIN = _module.DOMAIN
    Wordings = _module.Wordings
    default_wordings = _module.default_wordings
    parse = _module.parse
