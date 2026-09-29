"""Quality evaluation set (품질 평가 세트) for the INSIA pipeline.

Cases live in ``evals/cases/*.json`` (see ``evals/README.md``). The CLI is
``insia eval list | run | compare``:

- ``cases``: the case format and its validation,
- ``grounding``: number claims vs the research pack / profile / documents, citations, placeholders,
- ``graders``: format/brand checks, process metrics and the ``must``/``should`` assertions,
- ``runner``: runs cases through the real pipeline (mock or live, temporary workspace per run),
- ``compare`` / ``report``: baseline deltas and the Korean ``report.md``,
- ``estimate``: the live cost estimate printed before any paid call,
- ``judge``: optional pairwise LLM judge (costs money, off by default, never used in tests).
"""

from .cases import Assertion, CaseError, EvalCase, load_cases
from .compare import compare_summaries, render_compare
from .runner import EvalOptions, load_summary, run_eval

__all__ = [
    "Assertion",
    "CaseError",
    "EvalCase",
    "EvalOptions",
    "compare_summaries",
    "load_cases",
    "load_summary",
    "render_compare",
    "run_eval",
]
