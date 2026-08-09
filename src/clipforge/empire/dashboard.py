"""The one dashboard.

Four headline numbers, a leaderboard, and the three things most likely to be
about to go wrong. Ordered by what an operator running fifty channels can
actually act on in the two minutes they will give it.

**Alerts come before totals.** A dashboard that leads with cumulative views
teaches its reader that nothing on it is urgent. What matters at this scale is
the small number of channels that stopped, ran out of budget, lost their
credentials, or are about to lose a licence — and those are invisible in an
aggregate by construction, because forty-nine healthy channels drown one dead
one.

**Every number is scoped to the viewer.** The dashboard takes a `user_id` and
resolves what that user may see through the directory. An agency's client
opening this must see their brand and no other, and the safe place to enforce
that is where the data is fetched rather than where it is rendered.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..publish.types import Platform, ensure_utc
from .economics import Economics
from .rollup import ChannelLine, Growth, Totals


class Severity(str, enum.Enum):
    CRITICAL = "critical"   # revenue or publishing has stopped
    WARNING = "warning"     # will stop soon
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Alert:
    severity: Severity
    scope: str            # channel, brand or "empire"
    title: str
    detail: str
    #: What to do, not just what happened.
    action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "scope": self.scope,
            "title": self.title,
            "detail": self.detail,
            "action": self.action,
        }


_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}


@dataclass(slots=True)
class Dashboard:
    """Everything one user should see, in one payload."""

    tenant_name: str
    user_email: str
    role: str
    generated_at: datetime
    period_days: int = 7

    totals: Totals | None = None
    growth: list[Growth] = field(default_factory=list)
    economics: Economics | None = None
    leaderboard: list[ChannelLine] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    capacity: dict[str, Any] = field(default_factory=dict)
    scope_note: str = ""

    @property
    def critical(self) -> list[Alert]:
        return [a for a in self.alerts if a.severity is Severity.CRITICAL]

    def sorted_alerts(self) -> list[Alert]:
        return sorted(self.alerts, key=lambda a: _SEVERITY_ORDER[a.severity])

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant_name,
            "user": {"email": self.user_email, "role": self.role},
            "generated_at": self.generated_at.isoformat(),
            "period_days": self.period_days,
            "scope_note": self.scope_note,
            "alerts": [a.to_dict() for a in self.sorted_alerts()],
            "totals": self.totals.to_dict() if self.totals else None,
            "growth": [g.to_dict() for g in self.growth],
            "economics": self.economics.to_dict() if self.economics else None,
            "leaderboard": [line.to_dict() for line in self.leaderboard],
            "capacity": self.capacity,
        }

    def render(self, width: int = 78, rows: int = 8) -> str:
        """Plain text, for a terminal or an email."""
        rule = "═" * width
        thin = "─" * width
        out: list[str] = [
            rule,
            f"  {self.tenant_name.upper()} — EMPIRE",
            f"  {self.user_email} ({self.role})   "
            f"last {self.period_days} days   "
            f"{self.generated_at:%d %b %Y %H:%M} UTC",
            rule,
        ]
        if self.scope_note:
            out.append(f"  {self.scope_note}")

        alerts = self.sorted_alerts()
        if alerts:
            out += ["", f"  NEEDS ATTENTION ({len(self.critical)} critical)", ""]
            marks = {Severity.CRITICAL: "🔴", Severity.WARNING: "🟠",
                     Severity.INFO: "🔵"}
            for alert in alerts[:8]:
                out.append(f"   {marks[alert.severity]} {alert.title}")
                out.append(f"      {alert.detail}")
                if alert.action:
                    out.append(f"      → {alert.action}")
            if len(alerts) > 8:
                out.append(f"      … and {len(alerts) - 8} more")

        if self.totals:
            totals = self.totals
            out += ["", thin, "  TOTALS", ""]
            out.append(
                f"    {'uploads':<14}{totals.uploads:>14,}"
                f"        {'channels':<12}{totals.channels:>8,}"
            )
            out.append(
                f"    {'views':<14}{totals.views:>14,}"
                f"        {'brands':<12}{totals.brands:>8,}"
            )
            out.append(
                f"    {'subscribers':<14}{totals.subscribers:>14,}"
                f"        {'shares':<12}{totals.shares:>8,}"
            )
            if self.economics:
                gross = self.economics.gross_revenue_cents / 100
                net = self.economics.net_cents / 100
                out.append(
                    f"    {'revenue':<14}{'$' + format(gross, ',.0f'):>14}"
                    f"        {'net':<12}{'$' + format(net, ',.0f'):>8}"
                )
            spread = totals.views_concentration
            if spread:
                out += [
                    "",
                    f"    top channel {spread.top_1_share * 100:>5.0f}% of views"
                    f"    top 10% {spread.top_10pct_share * 100:>5.0f}%"
                    f"    dormant {spread.dormant}",
                    f"    ⓘ {spread.verdict}",
                ]

        if self.growth:
            out += ["", thin, "  GROWTH", ""]
            for entry in self.growth:
                out.append(f"    {entry.describe()}")

        if self.economics and self.economics.notes:
            out += ["", thin, "  ECONOMICS", ""]
            for note in self.economics.notes:
                out.append(f"    · {note}")

        if self.leaderboard:
            out += ["", thin, "  CHANNELS", "",
                    f"    {'CHANNEL':<20}{'BRAND':<16}"
                    f"{'UPLOADS':>9}{'VIEWS':>12}{'PER UPLOAD':>12}"]
            for line in self.leaderboard[:rows]:
                out.append(
                    f"    {line.channel_name[:19]:<20}{line.brand_name[:15]:<16}"
                    f"{line.uploads:>9,}{line.views:>12,}"
                    f"{line.views_per_upload:>12,.0f}"
                )
            if len(self.leaderboard) > rows:
                out.append(f"    … {len(self.leaderboard) - rows} more channels")

        if self.capacity:
            out += ["", thin, "  CAPACITY", ""]
            out.append(
                f"    ceiling {self.capacity.get('ceiling_per_day', 0):,}/day"
                f"   target {self.capacity.get('target_per_day', 0):,}/day"
                f"   {'ok' if self.capacity.get('feasible') else 'SHORT'}"
            )
            mix = self.capacity.get("forced_mix", {})
            if mix:
                out.append("    forced mix: " + "  ".join(
                    f"{k} {v:,}" for k, v in sorted(mix.items())
                ))

        out += ["", rule, ""]
        return "\n".join(out)
