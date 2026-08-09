"""Publishing system demo.

    python demo/run_publish_demo.py             # connect, schedule, run
    python demo/run_publish_demo.py --calendar  # the content calendar
    python demo/run_publish_demo.py --oauth     # the connection flows
    python demo/run_publish_demo.py --retry     # failure classification
    python demo/run_publish_demo.py --dst       # what DST does to a schedule
    python demo/run_publish_demo.py --json

Runs entirely offline. Every platform reply is scripted, so the state machines
are exercised for real without credentials or network.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from clipforge.publish import (  # noqa: E402
    Account,
    ClientCredentials,
    MediaAsset,
    Platform,
    PostSpec,
    PostState,
    PublishingSystem,
    RecordingTransport,
    Response,
    TokenSet,
    Visibility,
    authorization_url,
    classify,
    dst_report,
    limits_for,
    monthly_on,
    weekdays_at,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


def build_system() -> PublishingSystem:
    system = PublishingSystem(timezone="Europe/Amsterdam")

    accounts = [
        # Audited and verified — genuinely automated.
        Account("yt-main", Platform.YOUTUBE, "org1", handle="@clipforge",
                external_id="UCabc123", timezone="Europe/Amsterdam",
                direct_post_approved=True),
        # Not yet audited: this one degrades to a draft in the inbox.
        Account("tt-main", Platform.TIKTOK, "org1", handle="@clipforge",
                external_id="open_id_xyz", timezone="Europe/Amsterdam",
                direct_post_approved=False),
        # Business account, so publishing works.
        Account("ig-main", Platform.INSTAGRAM, "org1", handle="@clipforge",
                external_id="17841400000000000",
                timezone="Europe/Amsterdam", business_account=True),
    ]

    horizons = {
        Platform.YOUTUBE: 3650,
        Platform.TIKTOK: 365,
        Platform.INSTAGRAM: 60,      # the one that bites
    }

    for account in accounts:
        system.connect(account, TokenSet(
            account_id=account.account_id,
            platform=account.platform,
            access_token=f"at-{account.account_id}",
            refresh_token=f"rt-{account.account_id}",
            expires_at=NOW + timedelta(hours=1),
            refresh_valid_until=NOW + timedelta(days=horizons[account.platform]),
            obtained_at=NOW,
        ))
    return system


def make_specs(count: int, public_url: bool = False) -> list[PostSpec]:
    specs = []
    for index in range(count):
        specs.append(PostSpec(
            asset=MediaAsset(
                asset_id=f"clip-{index:03d}",
                path=f"/renders/clip-{index:03d}.mp4",
                public_url=(
                    f"https://cdn.clipforge.test/clip-{index:03d}.mp4"
                    if public_url else ""
                ),
                size_bytes=18 * 1024**2,
                duration_s=28.0,
            ),
            title=f"The raise was the mistake — part {index + 1}",
            caption="What I have never told anyone about the raise",
            hashtags=("founders", "startup"),
            visibility=Visibility.PUBLIC,
        ))
    return specs


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def show_readiness(system: PublishingSystem) -> None:
    section("ACCOUNT READINESS — read this before believing 'automated'")
    for report in system.readiness():
        mark = "✓" if report.automated else "✗"
        print(f"\n  {mark} {report.platform.value:<10} {report.account_id}")
        print(f"      {report.posts_per_day}/day ({report.quota_scope}-scoped)"
              f"   server-side scheduling: "
              f"{'yes' if report.server_side_scheduling else 'no'}"
              f"   credentials good for {report.safe_horizon_days}d")
        if report.blocker:
            print(f"      ⚠ degrades to: {report.degraded_to}")
            print(f"        {report.blocker}")
        for note in report.notes:
            print(f"      · {note}")


def scripted_transport(platform: Platform, chunks: int = 3) -> RecordingTransport:
    """A scripted happy path for each platform's protocol."""
    if platform is Platform.YOUTUBE:
        responses = [Response(200, {"Location": "https://upload/session/abc"})]
        # Google answers each chunk with 308 + how much it holds.
        held = 0
        chunk = 8 * 1024**2
        size = 18 * 1024**2
        while held + chunk < size:
            held += chunk
            responses.append(Response(308, {"Range": f"bytes=0-{held - 1}"}))
        responses.append(Response(200, {}, {"id": "yt_video_9f21"}))
        return RecordingTransport(responses)

    if platform is Platform.TIKTOK:
        responses = [
            Response(200, {}, {"data": {"publish_id": "pub_7731",
                                        "upload_url": "https://upload.tiktok/x"}}),
        ]
        responses += [Response(200) for _ in range(chunks)]
        responses += [
            Response(200, {}, {"data": {"status": "PROCESSING_UPLOAD"}}),
            Response(200, {}, {"data": {"status": "SEND_TO_USER_INBOX"}}),
        ]
        return RecordingTransport(responses)

    return RecordingTransport([
        Response(200, {}, {"id": "container_5512"}),
        Response(200, {}, {"status_code": "IN_PROGRESS"}),
        Response(200, {}, {"status_code": "FINISHED"}),
        Response(200, {}, {"id": "ig_media_8842"}),
    ])


def run_publishing(system: PublishingSystem) -> None:
    section("PUBLISHING — one post per platform, scripted end to end")

    for account_id, platform in (
        ("yt-main", Platform.YOUTUBE),
        ("tt-main", Platform.TIKTOK),
        ("ig-main", Platform.INSTAGRAM),
    ):
        spec = make_specs(1, public_url=True)[0]
        post = system.schedule(account_id, spec, NOW + timedelta(minutes=5))

        transport = scripted_transport(platform)
        result = system.run_post(post, transport, now=NOW + timedelta(minutes=6))

        print(f"\n  {platform.value:<10} {result.state.value:<12} "
              f"{result.requests} requests"
              + (f"   → {result.remote_post_id}" if result.remote_post_id else "")
              + ("   [DRAFT — a human must finish it]" if result.draft else ""))
        for request in transport.sent:
            byte_range = (
                f"  bytes {request.byte_range[0]}-{request.byte_range[1]}"
                if request.byte_range else ""
            )
            print(f"      {request.method:<5} {request.description}{byte_range}")


def run_calendar(system: PublishingSystem) -> None:
    section("CONTENT CALENDAR — a quarter of weekday posts")

    rule = weekdays_at(17, 0, "Europe/Amsterdam")
    print(f"\n  rule        {rule.describe()}")

    specs = make_specs(40)
    placed, rejected = system.schedule_bulk("yt-main", specs, rule, start=NOW)
    print(f"  bulk        {len(placed)} placed, {len(rejected)} rejected")
    for reason in rejected[:3]:
        print(f"                {reason}")

    monthly = monthly_on([1, -1], 9, 30, "Europe/Amsterdam",
                         series_id="monthly-recap")
    recap, refused = system.schedule_series(
        "ig-main", make_specs(1, public_url=True)[0], monthly,
        start=NOW, horizon_days=180,
    )
    print(f"  series      {monthly.describe()}")
    print(f"              {len(recap)} placed, {len(refused)} refused "
          f"over a 180-day horizon")
    if refused:
        print(f"\n  ⚠ SIX MONTHS ASKED FOR, {len(recap)} MONTHS BOOKED")
        print(f"    {refused[0]}")
        print(f"    …and {len(refused) - 1} more for the same reason. This is "
              f"the Instagram\n    60-day token horizon, caught at schedule "
              f"time rather than by\n    silently failing every post from "
              f"November onwards.")

    forecast = system.calendar.capacity_forecast(
        Platform.YOUTUBE, 200, NOW, accounts=["yt-main", "yt-second"]
    )
    print(f"\n  CAPACITY FORECAST for 200 YouTube uploads")
    print(f"    {forecast['per_day']}/day, {forecast['quota_scope']}-scoped")
    print(f"    {forecast['days_required']} days — finishes "
          f"{forecast['finishes_on']}")
    print(f"    {forecast['explanation']}")

    view = system.calendar.month_view(2026, 10)
    print(f"\n  OCTOBER 2026 — {view['total']} posts ({view['timezone']})")
    for day, entries in sorted(view["days"].items())[:6]:
        line = "  ".join(
            f"{e['time']} {e['platform'][:2]}" for e in entries
        )
        print(f"    {day}   {line}")
    if len(view["days"]) > 6:
        print(f"    … {len(view['days']) - 6} more days")

    conflicts = system.calendar.conflicts()
    if conflicts:
        print(f"\n  CONFLICTS ({len(conflicts)})")
        for conflict in conflicts[:4]:
            print(f"    [{conflict.kind}] {conflict.detail}")

    print(f"\n  {json.dumps(system.calendar.summary(), indent=2)[:400]}")


def run_oauth() -> None:
    section("OAUTH — connection flows, built not performed")

    credentials = ClientCredentials(
        client_id="demo-client", client_secret="secret",
        redirect_uri="https://clipforge.test/oauth/callback",
    )
    for platform in Platform:
        auth = authorization_url(platform, credentials)
        head, query = auth.url.split("?", 1)
        params = dict(p.split("=", 1) for p in query.split("&"))
        print(f"\n  {platform.value}")
        print(f"    {head}")
        for key in sorted(params):
            if key in ("code_challenge", "state"):
                print(f"      {key} = <{len(params[key])} chars>")
            else:
                print(f"      {key} = {params[key][:60]}")


def run_retry() -> None:
    section("FAILURE CLASSIFICATION — five outcomes, not one backoff curve")

    cases = [
        ("500 from the platform, nothing sent yet",
         Response(500), Platform.YOUTUBE, False, False),
        ("500 AFTER the publish call",
         Response(500), Platform.INSTAGRAM, True, False),
        ("timeout after the publish call",
         None, Platform.TIKTOK, True, True),
        ("401 — token rejected",
         Response(401, {}, {"error": {"code": "access_token_invalid"}}),
         Platform.TIKTOK, False, False),
        ("YouTube daily quota exhausted",
         Response(403, {}, {"error": {"errors": [{"reason": "quotaExceeded"}]}}),
         Platform.YOUTUBE, False, False),
        ("429 with Retry-After",
         Response(429, {"Retry-After": "900"}), Platform.INSTAGRAM, False, False),
        ("caption too long",
         Response(400, {}, {"error": {"code": "invalidDescription"}}),
         Platform.YOUTUBE, False, False),
    ]

    print()
    for label, response, platform, in_flight, timed_out in cases:
        decision = classify(response, 2, platform, NOW, key="demo",
                            timed_out=timed_out, already_in_flight=in_flight)
        wait = (
            f"{decision.delay_s / 3600:.1f}h" if decision.delay_s > 3600
            else f"{decision.delay_s:.0f}s" if decision.delay_s else "—"
        )
        flag = "  ⚠ UNSAFE TO REPEAT" if decision.unsafe_to_repeat else ""
        print(f"  {label:<42} {decision.disposition.value:<11} {wait:>7}{flag}")
        print(f"      {decision.reason}")


def run_dst() -> None:
    section("DAYLIGHT SAVING — the reason schedules are stored in local time")

    from clipforge.publish import daily

    rule = daily(2, 30, "America/New_York")
    print(f"\n  rule  {rule.describe()}")

    occurrences = rule.occurrences(
        datetime(2026, 3, 6, tzinfo=timezone.utc),
        datetime(2026, 3, 11, tzinfo=timezone.utc),
    )
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("America/New_York")
    print("\n  spring forward — 02:30 does not exist on the 8th")
    for moment in occurrences:
        local = moment.astimezone(zone)
        note = "  ← shifted" if local.hour != 2 else ""
        print(f"    {local:%Y-%m-%d %H:%M %Z}   (UTC {moment:%H:%M}){note}")

    autumn = daily(1, 30, "America/New_York").occurrences(
        datetime(2026, 10, 31, tzinfo=timezone.utc),
        datetime(2026, 11, 3, tzinfo=timezone.utc),
    )
    print("\n  fall back — 01:30 happens twice, and fires once")
    for moment in autumn:
        print(f"    {moment.astimezone(zone):%Y-%m-%d %H:%M %Z}   "
              f"(UTC {moment:%H:%M})")

    report = dst_report(rule, datetime(2026, 1, 1, tzinfo=timezone.utc),
                        datetime(2026, 12, 31, tzinfo=timezone.utc))
    print(f"\n  a year of this rule: {len(report.shifted)} shifted, "
          f"{len(report.skipped)} skipped, {len(report.doubled)} doubled")
    print("  Storing this rule as a UTC cron would silently move every post")
    print("  by an hour for half the year.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calendar", action="store_true")
    parser.add_argument("--oauth", action="store_true")
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--dst", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    system = build_system()

    if args.json:
        run_calendar(system)
        print(json.dumps(system.status(), indent=2))
        return 0

    if args.oauth:
        run_oauth()
        return 0
    if args.retry:
        run_retry()
        return 0
    if args.dst:
        run_dst()
        return 0
    if args.calendar:
        show_readiness(system)
        run_calendar(system)
        return 0

    show_readiness(system)
    run_publishing(system)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
