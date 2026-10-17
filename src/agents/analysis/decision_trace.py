"""The one writer of an analysis agent's ``decision-trace.json`` (#973).

The DynamoDB trace is a versioned contract (``DynamoDBDecisionTrace``, registered
for ``analysis-dynamodb/decision-trace.json`` in ``src/shared/contract_versioning.py``):
it is written through its model, so a trace the model refuses (a renamed field,
an unknown ``subtype``) is never stored, and the file says the contract's current
``contract_version`` (ADR-008 Decisions 1 and 2). The other engines' traces are
still plain dicts, registered for no contract, and are written as they are.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.contracts.dynamodb_decision_trace import DynamoDBDecisionTrace
from src.shared.contract_versioning import (
    contract_for_artifact,
    load_contract,
    stamp_artifact_version,
)


def write_decision_trace(store: Any, key: str, trace: Mapping[str, Any]) -> None:
    """Write ``trace`` to ``key``; a trace registered as a contract goes through it."""
    if contract_for_artifact(key) is DynamoDBDecisionTrace:
        typed = load_contract(DynamoDBDecisionTrace, trace, artifact=key)
        store.write_json(key, typed.model_dump(mode="json"))
    else:
        # Any other engine's trace: its path is registered for no contract, so the
        # stamp returns it unchanged (and a trace registered later is stamped, #1018).
        store.write_json(key, stamp_artifact_version(key, dict(trace)))
