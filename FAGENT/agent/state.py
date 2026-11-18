"""Serializable state for the layout repair graph."""
from typing import Any, TypedDict


class FagentState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    rundir: str
    candidate_path: str
    iteration: int
    validation: dict[str, Any] | None
    result: dict[str, Any]
    review: dict[str, Any]
    save_requested: bool
    review_finished: bool
