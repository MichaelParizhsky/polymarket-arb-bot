"""
Shadow testing framework for safe parameter validation.

Protocol:
  1. Meta-agent proposes parameter changes -> saved with status='proposed'
  2. ShadowTester runs proposed params in parallel (no capital) for 48h
  3. If simulated improvement > 2%, promote to 'validated'
  4. Only validated changes are eligible to go live
  5. Human can approve 'validated' changes via dashboard API

This prevents the meta-agent from making live changes based on 30 minutes of noisy data.
"""
from __future__ import annotations

import time
import json
from dataclasses import dataclass, field
from typing import Optional

from src.utils.logger import logger

try:
    from src.utils.database import (
        get_pending_parameter_proposals,
        validate_parameter_change,
        rollback_parameter_change,
    )
    _DB_AVAILABLE = True
except ImportError:
    _DB_AVAILABLE = False


# Minimum hours a proposal must be in shadow mode before validation
SHADOW_MIN_HOURS = 48.0
# Minimum improvement required to promote a proposal
MIN_IMPROVEMENT_PCT = 0.02
# Minimum number of signals needed in shadow period for statistical confidence
MIN_SHADOW_SIGNALS = 10


@dataclass
class ShadowResult:
    proposal_id: int
    param_name: str
    strategy: Optional[str]
    old_value: object
    new_value: object
    shadow_pnl: float = 0.0
    live_pnl_baseline: float = 0.0
    signal_count: int = 0
    hours_elapsed: float = 0.0
    status: str = "pending"  # pending, validated, rejected


class ShadowTester:
    """
    Tracks parameter proposals in shadow mode.
    Compares simulated PnL under proposed params vs live params.
    """

    def __init__(self) -> None:
        # proposal_id -> ShadowResult
        self._active_shadows: dict[int, ShadowResult] = {}
        self._started_at: dict[int, float] = {}

    def start_shadow(self, proposal: dict) -> None:
        """Begin tracking a new parameter proposal in shadow mode."""
        pid = proposal["id"]
        if pid in self._active_shadows:
            return  # Already tracking

        try:
            old_val = json.loads(proposal["old_value"])
            new_val = json.loads(proposal["new_value"])
        except (json.JSONDecodeError, TypeError):
            old_val = proposal.get("old_value")
            new_val = proposal.get("new_value")

        result = ShadowResult(
            proposal_id=pid,
            param_name=proposal["param_name"],
            strategy=proposal.get("strategy"),
            old_value=old_val,
            new_value=new_val,
        )
        self._active_shadows[pid] = result
        self._started_at[pid] = time.time()
        logger.info(
            f"Shadow test started for proposal #{pid}: "
            f"{proposal['param_name']} {old_val} -> {new_val} "
            f"(strategy: {proposal.get('strategy', 'global')})"
        )

    def record_shadow_signal(
        self,
        proposal_id: int,
        signal_pnl: float,
        live_pnl: float,
    ) -> None:
        """
        Record a signal outcome under both proposed and live parameters.
        signal_pnl: simulated PnL if proposed params were used
        live_pnl: actual PnL that occurred under live params
        """
        if proposal_id not in self._active_shadows:
            return
        result = self._active_shadows[proposal_id]
        result.shadow_pnl += signal_pnl
        result.live_pnl_baseline += live_pnl
        result.signal_count += 1

    def evaluate_all(self) -> list[ShadowResult]:
        """
        Evaluate all active shadow tests. Promote or reject each.
        Returns list of evaluated results.
        """
        evaluated = []
        to_remove = []

        for pid, result in self._active_shadows.items():
            elapsed_hours = (time.time() - self._started_at[pid]) / 3600
            result.hours_elapsed = elapsed_hours

            # Not enough time elapsed
            if elapsed_hours < SHADOW_MIN_HOURS:
                continue

            # Not enough signals for statistical confidence
            if result.signal_count < MIN_SHADOW_SIGNALS:
                logger.warning(
                    f"Shadow #{pid} ({result.param_name}): only {result.signal_count} signals "
                    f"after {elapsed_hours:.1f}h — need {MIN_SHADOW_SIGNALS}. Extending shadow period."
                )
                # Extend — don't reject yet, just wait
                continue

            # Compute improvement
            if result.live_pnl_baseline != 0:
                improvement = (result.shadow_pnl - result.live_pnl_baseline) / abs(result.live_pnl_baseline)
            else:
                improvement = 0.0

            if improvement >= MIN_IMPROVEMENT_PCT and _DB_AVAILABLE:
                validate_parameter_change(pid, improvement)
                result.status = "validated"
                logger.info(
                    f"Shadow #{pid} VALIDATED: {result.param_name} improvement {improvement:.1%} "
                    f"over {elapsed_hours:.1f}h ({result.signal_count} signals). "
                    f"Ready for human approval."
                )
            else:
                if _DB_AVAILABLE:
                    rollback_parameter_change(pid)
                result.status = "rejected"
                logger.info(
                    f"Shadow #{pid} REJECTED: {result.param_name} improvement {improvement:.1%} "
                    f"< required {MIN_IMPROVEMENT_PCT:.1%} after {elapsed_hours:.1f}h."
                )

            evaluated.append(result)
            to_remove.append(pid)

        for pid in to_remove:
            del self._active_shadows[pid]
            del self._started_at[pid]

        return evaluated

    def load_pending_proposals(self) -> None:
        """Load any pending proposals from the database and start shadow testing them."""
        if not _DB_AVAILABLE:
            return
        proposals = get_pending_parameter_proposals()
        for proposal in proposals:
            self.start_shadow(proposal)
        if proposals:
            logger.info(f"Loaded {len(proposals)} pending parameter proposals into shadow testing")

    def get_status(self) -> list[dict]:
        """Return current shadow test status for dashboard display."""
        now = time.time()
        return [
            {
                "proposal_id": r.proposal_id,
                "param_name": r.param_name,
                "strategy": r.strategy,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "hours_elapsed": round((now - self._started_at[pid]) / 3600, 1),
                "hours_remaining": max(0, round(SHADOW_MIN_HOURS - (now - self._started_at[pid]) / 3600, 1)),
                "signal_count": r.signal_count,
                "shadow_pnl": round(r.shadow_pnl, 4),
                "live_baseline_pnl": round(r.live_pnl_baseline, 4),
                "status": r.status,
            }
            for pid, r in self._active_shadows.items()
        ]
