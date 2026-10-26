"""Soft, in-process budget tracking for active provider streams.

The production runbook uses one Gunicorn worker with multiple threads.  This
module deliberately shares live estimates only inside that worker; completed
costs remain authoritative in SQLite.  If deployment moves to multiple
workers, this ledger must be replaced with a shared store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading


@dataclass
class _ParticipantBudget:
    credit_limit_usd: float
    durable_used_usd: float
    estimates: dict[str, float] = field(default_factory=dict)


class LiveBudgetLedger:
    """Thread-safe estimates for calls that have not reached SQLite usage yet."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._participants: dict[str, _ParticipantBudget] = {}

    def register(
        self,
        study_id: str,
        call_id: str,
        credit_limit_usd: float,
        durable_used_usd: float,
        estimated_cost_usd: float,
    ) -> None:
        with self._lock:
            state = self._participants.get(study_id)
            if state is None:
                state = _ParticipantBudget(
                    max(0.0, float(credit_limit_usd)),
                    max(0.0, float(durable_used_usd)),
                )
                self._participants[study_id] = state
            else:
                state.credit_limit_usd = min(
                    state.credit_limit_usd,
                    max(0.0, float(credit_limit_usd)),
                )
                # Calls register while holding SQLite's write transaction, so a
                # larger durable total is a strictly newer completed-cost view.
                state.durable_used_usd = max(
                    state.durable_used_usd,
                    max(0.0, float(durable_used_usd)),
                )
            state.estimates[call_id] = max(0.0, float(estimated_cost_usd))

    def set_estimated_cost(
        self, study_id: str, call_id: str, estimated_cost_usd: float
    ) -> bool:
        """Update one call and report whether the shared live pool is exhausted."""
        with self._lock:
            state = self._participants.get(study_id)
            if state is None or call_id not in state.estimates:
                return False
            state.estimates[call_id] = max(0.0, float(estimated_cost_usd))
            return (
                state.durable_used_usd + sum(state.estimates.values())
                >= state.credit_limit_usd
            )

    def settle(self, study_id: str, call_id: str, durable_used_usd: float) -> None:
        """Reconcile one call to the latest completed-cost total from SQLite."""
        with self._lock:
            state = self._participants.get(study_id)
            if state is None or call_id not in state.estimates:
                return
            state.estimates.pop(call_id, None)
            state.durable_used_usd = max(
                state.durable_used_usd,
                max(0.0, float(durable_used_usd)),
            )
            if not state.estimates:
                self._participants.pop(study_id, None)

    def discard(self, study_id: str, call_id: str) -> None:
        """Remove a failed call that will not be charged."""
        with self._lock:
            state = self._participants.get(study_id)
            if state is None:
                return
            state.estimates.pop(call_id, None)
            if not state.estimates:
                self._participants.pop(study_id, None)
