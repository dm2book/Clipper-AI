"""Revenue, cost, and whether the empire makes money.

This file exists because the two numbers needed to answer that are already in
this repository, and multiplying them out gives an answer nobody building an
automated clip farm wants to hear.

**Cost is known.** `factory.pipeline.ITEM_COST_CENTS` is 191 cents per clip —
transcription, detection, hooks, captions, render, schedule. At 500 uploads a
day that is $955 a day, roughly $29,000 a month.

**Short-form ad revenue is worse than most people model.** Three facts, and the
second is the one that surprises:

- YouTube Shorts pays roughly $0.02–0.07 per thousand views. Long-form YouTube
  pays ten to a hundred times that; Shorts revenue is pooled across the whole
  Shorts feed and divided by music licensing before it reaches a creator.
- TikTok's Creator Rewards programme **only pays on videos over one minute**.
  A fifteen-to-sixty-second clip — the entire output of this system — earns
  nothing. Not a low RPM. Zero.
- Instagram has no general Reels revenue share. Bonuses are invite-only and
  have been withdrawn in most markets.

So an empire producing five hundred clips a day earns almost all of its ad
revenue from the six that go to YouTube.

`break_even_views()` states the consequence as one number: how many views a
clip needs before it has paid for itself. It is usually in the tens of
thousands, against a typical few thousand. That is not a reason to abandon the
product — it is the reason the product's revenue line has to be sponsorship,
affiliate, lead generation into something else, or the subscription for the
tool itself. Automated clipping is a distribution business whose ad revenue is
a rounding error, and a dashboard that reports "total revenue" without saying
so is selling a fantasy.

Every figure below is an order-of-magnitude estimate with a version stamp.
Substitute the account's own measured RPM the moment there is one — `Economics`
takes them as parameters for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..factory.pipeline import ITEM_COST_CENTS
from ..publish.types import Platform

#: Bump when the rates below are re-checked. They move, and the direction over
#: the last few years has been down.
RATES_VERSION = "2026-08-verify-quarterly"

#: Revenue per thousand views, in cents. Deliberately conservative.
DEFAULT_RPM_CENTS: dict[Platform, float] = {
    # Shorts revenue share, after the music licensing deduction.
    Platform.YOUTUBE: 4.0,
    # Creator Rewards requires >60s. Every clip this system makes is shorter,
    # so the correct figure is zero rather than a small number.
    Platform.TIKTOK: 0.0,
    # No general Reels revenue share.
    Platform.INSTAGRAM: 0.0,
}

#: Share of gross platform revenue the creator actually keeps, after the
#: platform's own cut. Applied on top of RPM, which is usually quoted net —
#: kept separate so a measured gross figure can be substituted safely.
DEFAULT_PLATFORM_SHARE = 1.0


@dataclass(frozen=True, slots=True)
class RevenueStreams:
    """Everything that can pay for an empire, in cents per month.

    Ad revenue is one line of five, and at this volume it is rarely the
    largest. The others are here because a dashboard reporting only ad revenue
    tells a customer their business is failing when it may be thriving.
    """

    sponsorship_cents: int = 0
    affiliate_cents: int = 0
    #: Revenue attributed to traffic this content sent somewhere else.
    own_product_cents: int = 0
    #: What the operator charges clients, for an agency.
    services_cents: int = 0

    @property
    def non_ad_total(self) -> int:
        return (
            self.sponsorship_cents + self.affiliate_cents
            + self.own_product_cents + self.services_cents
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sponsorship_cents": self.sponsorship_cents,
            "affiliate_cents": self.affiliate_cents,
            "own_product_cents": self.own_product_cents,
            "services_cents": self.services_cents,
            "non_ad_total_cents": self.non_ad_total,
        }


@dataclass(frozen=True, slots=True)
class UnitEconomics:
    """The per-clip picture, which is where the problem is visible."""

    cost_cents: int
    views_per_clip: float
    ad_revenue_cents: float
    break_even_views: float

    @property
    def margin_cents(self) -> float:
        return self.ad_revenue_cents - self.cost_cents

    @property
    def ad_covers_cost(self) -> bool:
        return self.ad_revenue_cents >= self.cost_cents

    @property
    def views_multiple(self) -> float:
        """How many times more views a clip needs to pay for itself."""
        if self.views_per_clip <= 0:
            return float("inf")
        return self.break_even_views / self.views_per_clip

    def to_dict(self) -> dict[str, Any]:
        # `break_even_views` is legitimately infinite whenever the blended RPM
        # is zero, which is the *normal* case for a portfolio posting only to
        # TikTok and Instagram. `round(inf)` raises OverflowError and JSON has
        # no infinity, so both become null — the honest encoding of "no number
        # of views repays this from ads".
        finite = self.break_even_views != float("inf")
        return {
            "cost_cents": self.cost_cents,
            "views_per_clip": round(self.views_per_clip, 1),
            "ad_revenue_cents": round(self.ad_revenue_cents, 3),
            "margin_cents": round(self.margin_cents, 2),
            "break_even_views": (
                round(self.break_even_views) if finite else None
            ),
            "break_even_unreachable": not finite,
            "views_multiple": (
                None if self.views_multiple == float("inf")
                else round(self.views_multiple, 1)
            ),
            "ad_covers_cost": self.ad_covers_cost,
        }


@dataclass(frozen=True, slots=True)
class Economics:
    """A month of empire, costed."""

    uploads: int
    views: int
    ad_revenue_cents: int
    other_revenue_cents: int
    production_cost_cents: int
    subscription_cents: int = 0
    unit: UnitEconomics | None = None
    by_platform: dict[str, int] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def gross_revenue_cents(self) -> int:
        return self.ad_revenue_cents + self.other_revenue_cents

    @property
    def total_cost_cents(self) -> int:
        return self.production_cost_cents + self.subscription_cents

    @property
    def net_cents(self) -> int:
        return self.gross_revenue_cents - self.total_cost_cents

    @property
    def profitable(self) -> bool:
        return self.net_cents > 0

    @property
    def ad_share(self) -> float:
        return (
            self.ad_revenue_cents / self.gross_revenue_cents
            if self.gross_revenue_cents else 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rates_version": RATES_VERSION,
            "uploads": self.uploads,
            "views": self.views,
            "revenue_cents": {
                "ad": self.ad_revenue_cents,
                "other": self.other_revenue_cents,
                "gross": self.gross_revenue_cents,
                "ad_share": round(self.ad_share, 3),
                "by_platform": dict(sorted(self.by_platform.items())),
            },
            "cost_cents": {
                "production": self.production_cost_cents,
                "subscription": self.subscription_cents,
                "total": self.total_cost_cents,
            },
            "net_cents": self.net_cents,
            "profitable": self.profitable,
            "unit": self.unit.to_dict() if self.unit else None,
            "notes": list(self.notes),
        }


def ad_revenue_cents(
    views_by_platform: dict[Platform, int],
    rpm_cents: dict[Platform, float] | None = None,
    platform_share: float = DEFAULT_PLATFORM_SHARE,
) -> tuple[int, dict[str, int]]:
    """Ad revenue from views, per platform."""
    rates = rpm_cents or DEFAULT_RPM_CENTS
    breakdown: dict[str, int] = {}
    total = 0.0

    for platform, views in views_by_platform.items():
        rate = rates.get(platform, 0.0)
        earned = (views / 1000.0) * rate * platform_share
        breakdown[platform.value] = int(round(earned))
        total += earned

    return int(round(total)), breakdown


def break_even_views(
    cost_cents: int = ITEM_COST_CENTS,
    rpm_cents: float = DEFAULT_RPM_CENTS[Platform.YOUTUBE],
) -> float:
    """Views one clip needs before its ad revenue covers its production.

    The single most clarifying number in this file. At 191 cents a clip and a
    4-cent Shorts RPM it is nearly fifty thousand views — against a typical
    few thousand — and on TikTok or Instagram, where the RPM for a sub-minute
    clip is zero, it is infinite.
    """
    if rpm_cents <= 0:
        return float("inf")
    return cost_cents / rpm_cents * 1000.0


def blended_rpm_cents(
    views_by_platform: dict[Platform, int],
    rpm_cents: dict[Platform, float] | None = None,
) -> float:
    """Revenue per thousand views across the mix the empire actually posts.

    The right rate to judge an empire by, and much lower than any single
    platform's headline figure — because most of the reach is on platforms
    that pay nothing for a sub-minute clip. Using the dominant platform's own
    RPM instead gives either a flattering number or a literal infinity,
    depending on which platform happens to be dominant.
    """
    views = sum(views_by_platform.values())
    if not views:
        return 0.0
    total, _ = ad_revenue_cents(views_by_platform, rpm_cents)
    return total / (views / 1000.0)


def unit_economics(
    views_per_clip: float,
    rpm: float,
    cost_cents: int = ITEM_COST_CENTS,
) -> UnitEconomics:
    """Per-clip economics against an effective revenue-per-thousand rate."""
    return UnitEconomics(
        cost_cents=cost_cents,
        views_per_clip=views_per_clip,
        ad_revenue_cents=(views_per_clip / 1000.0) * rpm,
        break_even_views=break_even_views(cost_cents, rpm),
    )


def month(
    uploads: int,
    views_by_platform: dict[Platform, int],
    streams: RevenueStreams | None = None,
    cost_per_upload_cents: int = ITEM_COST_CENTS,
    subscription_cents: int = 0,
    rpm_cents: dict[Platform, float] | None = None,
) -> Economics:
    """Cost a month of the empire, and say plainly whether it works."""
    streams = streams or RevenueStreams()
    rates = rpm_cents or DEFAULT_RPM_CENTS

    ads, breakdown = ad_revenue_cents(views_by_platform, rates)
    views = sum(views_by_platform.values())
    production = uploads * cost_per_upload_cents

    per_clip = views / uploads if uploads else 0.0
    blended = blended_rpm_cents(views_by_platform, rates)
    unit = unit_economics(per_clip, blended, cost_per_upload_cents)

    notes: list[str] = []

    zero_rpm = sorted(
        platform.value for platform, views in views_by_platform.items()
        if views and rates.get(platform, 0.0) <= 0
    )
    if zero_rpm:
        zero_views = sum(
            v for p, v in views_by_platform.items() if rates.get(p, 0.0) <= 0
        )
        notes.append(
            f"{', '.join(zero_rpm)} contribute {zero_views:,} views and zero "
            f"ad revenue — TikTok's Creator Rewards only pays on videos over "
            f"a minute, and Instagram has no general Reels revenue share. "
            f"{zero_views / views * 100:.0f}% of this reach is unmonetised by "
            f"ads."
        )

    if unit.views_per_clip > 0 and blended <= 0:
        notes.append(
            f"Blended RPM is zero: nothing in this platform mix pays for a "
            f"sub-minute clip. Every cent of the "
            f"${production / 100:,.0f} production cost has to come from "
            f"somewhere other than ads."
        )
    elif not unit.ad_covers_cost and unit.views_per_clip > 0:
        notes.append(
            f"Blended RPM across this mix is {blended:.3f}c per thousand. "
            f"Each clip costs {cost_per_upload_cents}c and needs "
            f"{unit.break_even_views:,.0f} views to repay that from ads; it "
            f"averages {unit.views_per_clip:,.0f} — short by "
            f"{unit.views_multiple:,.0f}x. Ad revenue does not fund automated "
            f"production at this volume and is not meant to."
        )

    gross = ads + streams.non_ad_total
    if gross and ads / gross < 0.25:
        notes.append(
            f"Ads are {ads / gross * 100:.0f}% of revenue. The business is "
            f"whatever the other {100 - ads / gross * 100:.0f}% is — "
            f"sponsorship, affiliate, or traffic sent somewhere that "
            f"monetises. Report that line, not the ad line."
        )

    return Economics(
        uploads=uploads, views=views,
        ad_revenue_cents=ads, other_revenue_cents=streams.non_ad_total,
        production_cost_cents=production,
        subscription_cents=subscription_cents,
        unit=unit, by_platform=breakdown, notes=tuple(notes),
    )


def required_non_ad_revenue_cents(
    uploads: int, views_by_platform: dict[Platform, int],
    cost_per_upload_cents: int = ITEM_COST_CENTS,
    rpm_cents: dict[Platform, float] | None = None,
) -> int:
    """Non-ad revenue needed to break even.

    The number a business plan should start from, rather than arrive at.
    """
    ads, _ = ad_revenue_cents(views_by_platform, rpm_cents)
    return max(0, uploads * cost_per_upload_cents - ads)
