"""Provider-neutral, conservative AI spending guard."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


class BudgetExceeded(RuntimeError):
    """Raised before a request when the configured allowance cannot cover it."""


@dataclass
class UsageBudget:
    monthly_limit_usd: float = 20.0
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    period: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m"))

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.monthly_limit_usd - self.spent_usd - self.reserved_usd)

    @property
    def percent_used(self) -> float:
        if self.monthly_limit_usd <= 0:
            return 100.0
        return min(100.0, ((self.spent_usd + self.reserved_usd) / self.monthly_limit_usd) * 100)

    def reserve(self, estimated_usd: float) -> None:
        estimate = max(0.0, float(estimated_usd))
        if estimate > self.remaining_usd + 1e-9:
            raise BudgetExceeded(
                f"AI allowance is unavailable (remaining ${self.remaining_usd:.4f}, "
                f"request estimate ${estimate:.4f})."
            )
        self.reserved_usd += estimate

    def settle(self, actual_usd: float, reserved_usd: float = 0.0) -> None:
        """Replace a reservation with provider-reported or estimated actual cost."""
        self.reserved_usd = max(0.0, self.reserved_usd - max(0.0, reserved_usd))
        self.spent_usd += max(0.0, float(actual_usd))

    def reset(self, period: str) -> None:
        self.period = period
        self.spent_usd = 0.0
        self.reserved_usd = 0.0

