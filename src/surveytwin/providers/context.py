"""Which twin and stage a model call belongs to, for usage records."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator

_CALL_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "surveytwin_call_context", default={}
)


def current_call_context() -> dict[str, Any]:
    return _CALL_CONTEXT.get()


@contextmanager
def call_context(**values: Any) -> Iterator[None]:
    token = _CALL_CONTEXT.set({**_CALL_CONTEXT.get(), **values})
    try:
        yield
    finally:
        _CALL_CONTEXT.reset(token)
