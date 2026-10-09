"""The texts every prompt is built from.

``DEFAULT_SYSTEM_INSTRUCTION`` is the only editable part (``configure(system_instruction=...)``).
The answer rules (``SIMULATION_FORMAT_CONTRACT``) and the response shape are fixed: the
answer reader finds answers by that shape and its keys (decided on 2026-09-25).
"""

from __future__ import annotations

# Version of the prompt text. Raised whenever any prompt text changes; recorded in
# every result. "1" is the website's prompt text on main b1679f8b, with the
# bank safety line always present (every persona comes from a bank). "2": question
# text keeps its lines and each survey picture is sent where the
# survey shows it. "3": a conjoint task shows its alternatives as a
# table, and a MaxDiff pair is not asked again during a run while a
# rerun asks it with its rule. "4": a rerun asks a ranking again with its rule
# (a unique rank from 1 to N), and the rerun instruction says no rank may be
# given twice or skipped.
PROMPT_VERSION = "4"


def __getattr__(name: str):
    # Lazy, so importing surveytwin.prompts does not import the answer engine first.
    if name in {"DEFAULT_SYSTEM_INSTRUCTION", "SIMULATION_FORMAT_CONTRACT", "RERUN_INSTRUCTION"}:
        from ..answers import executor

        return getattr(executor, name)
    if name == "CT_SYSTEM_INSTRUCTION":
        from ..personas.wrapping import CT_SYSTEM_INSTRUCTION

        return CT_SYSTEM_INSTRUCTION
    raise AttributeError(name)
