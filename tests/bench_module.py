"""Imports scripts/bench_models.py, which is a script rather than a package module."""

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from wordings_module import DOMAIN, default_wordings

__all__ = ["GOOD", "bench"]

if TYPE_CHECKING:
    # mypy checks scripts/ as top-level modules, so the tests see the script's real types.
    import bench_models as bench
else:
    _SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bench_models.py"
    _spec = importlib.util.spec_from_file_location("bench_models", _SCRIPT)
    assert _spec
    assert _spec.loader
    bench = importlib.util.module_from_spec(_spec)
    # @dataclass resolves annotations through sys.modules, so register before executing.
    sys.modules["bench_models"] = bench
    _spec.loader.exec_module(bench)

# A rewrite that satisfies every check for a plan-first, independent-review draft. Its fixed
# wordings come from prompts/default.md, like the bench's own (#283).
_W = default_wordings()
GOOD = f"""<CONTEXT>
I am working as a Head of Data at Example Corp. I want a board paper.
</CONTEXT>

<GOAL>
A board-ready paper on collections model risk.
</GOAL>

<INSTRUCTIONS>
1/ {_W.plan}
2/ {_W.inputs}
3/ Analyse the NPL spike.
4/ {_W.independent.replace(DOMAIN, "credit risk")}
5/ {_W.judgment}
</INSTRUCTIONS>

<CONSTRAINTS>
Model rebuild.
</CONSTRAINTS>

<INPUTS>
NPL data.
</INPUTS>

<OUTPUTS>
{_W.outputs}
</OUTPUTS>"""
