"""The buy filters as a pure function: TokenReport -> Verdict.

Every check yields one of:

* PASS    - condition met
* WAIT    - not met *yet* (or data unavailable); re-check later
* REJECT  - a hard scam signal; never re-check
* UNKNOWN - the stage that measures it has not run yet

The overall verdict is REJECT if anything rejects, PASS only if everything
passes, and WAIT otherwise. Missing data is never a PASS.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sniper.analysis.report import TokenReport
from sniper.config import FiltersConfig


class Status(str, Enum):
    PASS = "PASS"
    WAIT = "WAIT"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True)
class Verdict:
    status: Status  # PASS, WAIT or REJECT
    checks: tuple[Check, ...]

    @property
    def blocking(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status in (Status.REJECT, Status.WAIT))

    @property
    def reason(self) -> str:
        bad = [c for c in self.checks if c.status == Status.REJECT] or list(self.blocking) or [
            c for c in self.checks if c.status == Status.UNKNOWN]
        return "; ".join(f"{c.name}: {c.detail}" for c in bad) or "all checks passed"

    def to_json(self) -> list[dict]:
        return [{"name": c.name, "status": c.status.value, "detail": c.detail} for c in self.checks]


def _unavailable(report: TokenReport, stage: str, name: str) -> Check:
    if stage in report.errors:
        return Check(name, Status.WAIT, f"data unavailable ({report.errors[stage]})")
    return Check(name, Status.UNKNOWN, "not measured yet")


def evaluate(report: TokenReport, cfg: FiltersConfig) -> Verdict:
    checks: list[Check] = []
    add = checks.append

    # --- denylist -------------------------------------------------------
    if report.mint in cfg.denylist_mints:
        add(Check("denylist", Status.REJECT, "mint is on the denylist"))
    else:
        add(Check("denylist", Status.PASS, "not listed"))

    # --- mint account ---------------------------------------------------
    if not report.mint_loaded:
        for n in ("mint_authority", "freeze_authority", "token_extensions"):
            add(_unavailable(report, "mint", n))
    else:
        if cfg.require_mint_authority_revoked and report.mint_authority:
            add(Check("mint_authority", Status.REJECT, f"still set ({report.mint_authority})"))
        else:
            add(Check("mint_authority", Status.PASS, "revoked"))
        if cfg.require_freeze_authority_revoked and report.freeze_authority:
            add(Check("freeze_authority", Status.REJECT, f"still set ({report.freeze_authority})"))
        else:
            add(Check("freeze_authority", Status.PASS, "revoked"))
        bad = sorted(set(report.extensions) & set(cfg.forbidden_extensions))
        if bad:
            add(Check("token_extensions", Status.REJECT, "forbidden: " + ", ".join(bad)))
        else:
            add(Check("token_extensions", Status.PASS, "none forbidden"))

    # --- rugcheck ---------------------------------------------------------
    rc = cfg.rugcheck
    if not rc.enabled:
        add(Check("rugcheck", Status.PASS, "disabled"))
    elif report.rugcheck is None:
        add(_unavailable(report, "rugcheck", "rugcheck"))
    else:
        flagged = [n for n, level in report.rugcheck.risks if level in rc.reject_levels]
        score = report.rugcheck.score_normalised
        if flagged:
            add(Check("rugcheck", Status.REJECT, "risks: " + ", ".join(flagged)))
        elif rc.max_score_normalised is not None and score is not None and score > rc.max_score_normalised:
            add(Check("rugcheck", Status.REJECT, f"score {score} > {rc.max_score_normalised}"))
        else:
            add(Check("rugcheck", Status.PASS, f"score {score}"))

    # --- pool / liquidity -----------------------------------------------
    if report.pair_found is None:
        add(_unavailable(report, "dexscreener", "pool_active"))
        add(_unavailable(report, "dexscreener", "liquidity"))
    elif not report.pair_found or not report.liquidity_usd:
        add(Check("pool_active", Status.WAIT, "no pool with liquidity yet"))
        add(Check("liquidity", Status.WAIT, "no pool with liquidity yet"))
    else:
        add(Check("pool_active", Status.PASS, f"{report.dex_id} {report.pool_address}"))
        liq = report.liquidity_usd
        if liq >= cfg.min_liquidity_usd:
            add(Check("liquidity", Status.PASS, f"${liq:,.0f}"))
        else:
            add(Check("liquidity", Status.WAIT, f"${liq:,.0f} < ${cfg.min_liquidity_usd:,.0f}"))

    # --- holders ----------------------------------------------------------
    if report.holders is None:
        add(_unavailable(report, "holders", "holders"))
    elif report.holders >= cfg.min_holders:
        add(Check("holders", Status.PASS, f"{report.holders}{'+' if report.holders_capped else ''}"))
    else:
        add(Check("holders", Status.WAIT, f"{report.holders} < {cfg.min_holders}"))

    # --- concentration ----------------------------------------------------
    if report.top10_pct is None:
        add(_unavailable(report, "top10", "top10"))
    elif report.top10_pct < cfg.max_top10_pct:
        add(Check("top10", Status.PASS, f"{report.top10_pct}%"))
    else:
        add(Check("top10", Status.WAIT, f"{report.top10_pct}% >= {cfg.max_top10_pct}%"))

    # --- tradability ------------------------------------------------------
    if report.buy_route is None:
        add(_unavailable(report, "roundtrip", "tradable"))
    elif not report.buy_route or not report.sell_route:
        side = "buy" if not report.buy_route else "sell"
        add(Check("tradable", Status.WAIT, f"no {side} route on Jupiter"))
    elif report.roundtrip_loss_pct is not None and report.roundtrip_loss_pct > cfg.max_roundtrip_loss_pct:
        add(Check("tradable", Status.REJECT,
                  f"round-trip loses {report.roundtrip_loss_pct}% (> {cfg.max_roundtrip_loss_pct}%)"))
    else:
        add(Check("tradable", Status.PASS, f"round-trip loss {report.roundtrip_loss_pct}%"))

    statuses = {c.status for c in checks}
    if Status.REJECT in statuses:
        overall = Status.REJECT
    elif statuses == {Status.PASS}:
        overall = Status.PASS
    else:
        overall = Status.WAIT
    return Verdict(overall, tuple(checks))
