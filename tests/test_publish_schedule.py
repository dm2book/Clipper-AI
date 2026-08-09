"""Recurrence, DST correctness, the content calendar, and failure classification."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import _support  # noqa: F401  (path setup)

from clipforge.publish import (
    AmbiguousTime,
    ContentCalendar,
    Disposition,
    Frequency,
    MediaAsset,
    NonexistentTime,
    Platform,
    PostSpec,
    PostState,
    Recurrence,
    Response,
    ScheduledPost,
    WEEKDAYS,
    backoff_delay,
    classify,
    daily,
    dst_report,
    monthly_on,
    weekdays_at,
    weekly_on,
)
from clipforge.publish.retry import MAX_ATTEMPTS, MAX_BACKOFF_S, exhausted
from clipforge.publish.schedule import is_ambiguous, is_nonexistent, resolve_local

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
AMS = ZoneInfo("Europe/Amsterdam")


def at(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def through(year, month, day) -> datetime:
    """End of the given day. `occurrences` takes instants, not dates, so an
    end of midnight excludes everything later that day."""
    return datetime(year, month, day, 23, 59, 59, tzinfo=UTC)


def post(post_id: str, when: datetime, account="a1",
         platform=Platform.YOUTUBE, state=PostState.SCHEDULED) -> ScheduledPost:
    return ScheduledPost(
        post_id=post_id,
        account_id=account,
        platform=platform,
        spec=PostSpec(asset=MediaAsset(f"asset-{post_id}"), title=post_id),
        run_at=when,
        state=state,
    )


class TestDstDetection(unittest.TestCase):
    def test_spring_forward_hour_does_not_exist(self):
        self.assertTrue(is_nonexistent(datetime(2026, 3, 8, 2, 30, tzinfo=NY), NY))
        self.assertFalse(is_nonexistent(datetime(2026, 3, 8, 4, 30, tzinfo=NY), NY))

    def test_fall_back_hour_happens_twice(self):
        self.assertTrue(is_ambiguous(datetime(2026, 11, 1, 1, 30, tzinfo=NY), NY))
        self.assertFalse(is_ambiguous(datetime(2026, 11, 1, 3, 30, tzinfo=NY), NY))

    def test_nonexistent_can_be_skipped(self):
        moments = resolve_local(
            datetime(2026, 3, 8, 2, 30), NY, NonexistentTime.SKIP
        )
        self.assertEqual(moments, [])

    def test_nonexistent_shifts_to_the_landing_instant(self):
        moments = resolve_local(
            datetime(2026, 3, 8, 2, 30), NY, NonexistentTime.SHIFT
        )
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0].astimezone(NY).hour, 3)

    def test_ambiguous_fires_once_by_default(self):
        moments = resolve_local(datetime(2026, 11, 1, 1, 30), NY)
        self.assertEqual(len(moments), 1)

    def test_ambiguous_can_deliberately_fire_twice(self):
        moments = resolve_local(
            datetime(2026, 11, 1, 1, 30), NY,
            ambiguous=AmbiguousTime.BOTH,
        )
        self.assertEqual(len(moments), 2)
        self.assertNotEqual(moments[0], moments[1])

    def test_a_daily_rule_holds_local_time_across_a_transition(self):
        # The whole reason schedules are stored in local time. A UTC cron
        # would shift every one of these by an hour.
        rule = daily(17, 0, "America/New_York")
        moments = rule.occurrences(at(2026, 3, 6), at(2026, 3, 11))
        hours = {m.astimezone(NY).hour for m in moments}
        self.assertEqual(hours, {17})

        offsets = {m.astimezone(NY).utcoffset() for m in moments}
        self.assertEqual(len(offsets), 2, "the UTC offset must change, not "
                                          "the local hour")

    def test_fall_back_does_not_double_post(self):
        rule = daily(1, 30, "America/New_York")
        moments = rule.occurrences(at(2026, 10, 31), at(2026, 11, 3))
        self.assertEqual(len(moments), len(set(moments)))
        days = [m.astimezone(NY).date() for m in moments]
        self.assertEqual(len(days), len(set(days)), "one post per day")

    def test_dst_report_names_the_affected_occurrence(self):
        report = dst_report(
            daily(2, 30, "America/New_York"), at(2026, 1, 1), at(2026, 12, 31)
        )
        self.assertEqual(len(report.shifted), 1)
        self.assertFalse(report.clean)

    def test_a_schedule_clear_of_transitions_reports_clean(self):
        report = dst_report(
            daily(17, 0, "America/New_York"), at(2026, 1, 1), at(2026, 12, 31)
        )
        self.assertTrue(report.clean)

    def test_utc_schedules_are_never_affected(self):
        report = dst_report(daily(2, 30, "UTC"), at(2026, 1, 1), at(2026, 12, 31))
        self.assertTrue(report.clean)


class TestRecurrence(unittest.TestCase):
    def test_daily(self):
        moments = daily(9, 0, "UTC").occurrences(at(2026, 5, 1), through(2026, 5, 7))
        self.assertEqual(len(moments), 7)
        self.assertTrue(all(m.hour == 9 for m in moments))

    def test_daily_with_an_interval(self):
        rule = daily(9, 0, "UTC", starts_on=date(2026, 5, 1), interval=3)
        moments = rule.occurrences(at(2026, 5, 1), through(2026, 5, 10))
        self.assertEqual([m.day for m in moments], [1, 4, 7, 10])

    def test_weekdays_only(self):
        moments = weekdays_at(17, 0, "UTC").occurrences(
            at(2026, 5, 4), through(2026, 5, 10)
        )
        self.assertEqual(len(moments), 5)
        self.assertTrue(all(m.weekday() in WEEKDAYS for m in moments))

    def test_specific_weekdays(self):
        moments = weekly_on([0, 3], 12, 0, "UTC").occurrences(
            at(2026, 5, 1), through(2026, 5, 21)
        )
        self.assertTrue(all(m.weekday() in (0, 3) for m in moments))
        self.assertEqual(len(moments), 6)

    def test_fortnightly(self):
        rule = weekly_on([0], 12, 0, "UTC", interval=2,
                         starts_on=date(2026, 5, 4))
        moments = rule.occurrences(at(2026, 5, 1), through(2026, 6, 15))
        gaps = {(b - a).days for a, b in zip(moments, moments[1:])}
        self.assertEqual(gaps, {14})

    def test_multiple_times_per_day(self):
        rule = Recurrence(Frequency.DAILY, (time(9, 0), time(17, 30)), "UTC")
        moments = rule.occurrences(at(2026, 5, 1), through(2026, 5, 3))
        self.assertEqual(len(moments), 6)

    def test_monthly_on_a_fixed_day(self):
        moments = monthly_on([15], 9, 0, "UTC").occurrences(
            at(2026, 1, 1), through(2026, 6, 30)
        )
        self.assertEqual([m.month for m in moments], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(m.day == 15 for m in moments))

    def test_monthly_last_day_adapts_to_month_length(self):
        moments = monthly_on([-1], 9, 0, "UTC").occurrences(
            at(2026, 1, 1), through(2026, 4, 30)
        )
        self.assertEqual([m.day for m in moments], [31, 28, 31, 30])

    def test_a_31st_that_does_not_exist_is_skipped_not_rolled_forward(self):
        # Rolling into the 1st of the next month is the classic wrong answer:
        # it silently posts on a day the customer did not choose.
        moments = monthly_on([31], 9, 0, "UTC").occurrences(
            at(2026, 1, 1), through(2026, 4, 30)
        )
        self.assertEqual([(m.month, m.day) for m in moments],
                         [(1, 31), (3, 31)])

    def test_start_and_end_bounds(self):
        rule = daily(9, 0, "UTC", starts_on=date(2026, 5, 5),
                     ends_on=date(2026, 5, 8))
        moments = rule.occurrences(at(2026, 5, 1), through(2026, 5, 31))
        self.assertEqual([m.day for m in moments], [5, 6, 7, 8])

    def test_max_occurrences(self):
        rule = daily(9, 0, "UTC", max_occurrences=3)
        self.assertEqual(len(rule.occurrences(at(2026, 5, 1), at(2026, 5, 31))), 3)

    def test_next_after(self):
        rule = weekdays_at(17, 0, "UTC")
        following = rule.next_after(at(2026, 5, 2))   # a Saturday
        self.assertEqual(following.weekday(), 0)

    def test_occurrences_are_sorted_and_unique(self):
        rule = Recurrence(Frequency.DAILY, (time(9, 0), time(8, 0)), "UTC")
        moments = rule.occurrences(at(2026, 5, 1), through(2026, 5, 5))
        self.assertEqual(moments, sorted(moments))
        self.assertEqual(len(moments), len(set(moments)))

    def test_months_ahead(self):
        # The headline requirement: schedules that reach into next year.
        moments = weekdays_at(17, 0, "Europe/Amsterdam").occurrences(
            at(2026, 9, 1), at(2027, 3, 1)
        )
        self.assertGreater(len(moments), 120)
        hours = {m.astimezone(AMS).hour for m in moments}
        self.assertEqual(hours, {17}, "local hour must hold across the winter "
                                      "transition")

    def test_a_bad_timezone_fails_at_construction(self):
        with self.assertRaises(Exception):
            daily(9, 0, "Mars/Olympus_Mons")

    def test_a_recurrence_needs_a_time(self):
        with self.assertRaises(ValueError):
            Recurrence(Frequency.DAILY, (), "UTC")

    def test_describe_is_readable(self):
        self.assertEqual(
            weekdays_at(17, 0, "Europe/Amsterdam").describe(),
            "every Mon, Tue, Wed, Thu, Fri at 17:00 (Europe/Amsterdam)",
        )
        self.assertEqual(
            monthly_on([1, -1], 9, 30, "UTC").describe(),
            "every month on the 1st, last at 09:30 (UTC)",
        )


class TestCalendar(unittest.TestCase):
    def setUp(self):
        self.calendar = ContentCalendar(tz="Europe/Amsterdam")

    def test_between_filters_by_window_and_account(self):
        self.calendar.add(post("p1", at(2026, 5, 1, 9)))
        self.calendar.add(post("p2", at(2026, 5, 5, 9), account="a2"))
        self.assertEqual(len(self.calendar.between(at(2026, 5, 1), at(2026, 5, 10))), 2)
        self.assertEqual(
            len(self.calendar.between(at(2026, 5, 1), at(2026, 5, 10),
                                      account_id="a2")), 1)

    def test_due_returns_only_ready_posts(self):
        self.calendar.add(post("past", at(2026, 5, 1, 9)))
        self.calendar.add(post("future", at(2026, 6, 1, 9)))
        self.calendar.add(post("done", at(2026, 5, 1, 9),
                               state=PostState.PUBLISHED))
        due = self.calendar.due(at(2026, 5, 2))
        self.assertEqual([p.post_id for p in due], ["past"])

    def test_due_respects_a_retry_delay(self):
        retrying = post("r1", at(2026, 5, 1, 9), state=PostState.RETRYING)
        retrying.next_attempt_at = at(2026, 5, 1, 12)
        self.calendar.add(retrying)
        self.assertEqual(self.calendar.due(at(2026, 5, 1, 10)), ())
        self.assertEqual(len(self.calendar.due(at(2026, 5, 1, 13))), 1)

    def test_spacing_conflict(self):
        self.calendar.add(post("p1", at(2026, 5, 1, 9, 0)))
        self.calendar.add(post("p2", at(2026, 5, 1, 9, 30)))
        kinds = {c.kind for c in self.calendar.conflicts()}
        self.assertIn("spacing", kinds)

    def test_no_spacing_conflict_when_far_apart(self):
        self.calendar.add(post("p1", at(2026, 5, 1, 9, 0)))
        self.calendar.add(post("p2", at(2026, 5, 1, 14, 0)))
        self.assertEqual([c for c in self.calendar.conflicts()
                          if c.kind == "spacing"], [])

    def test_daily_cap_conflict(self):
        for index in range(8):
            self.calendar.add(
                post(f"p{index}", at(2026, 5, 1, 6 + index * 2),
                     account="ig1", platform=Platform.INSTAGRAM)
            )
        # Instagram allows 25/day, so eight is fine.
        self.assertEqual([c for c in self.calendar.conflicts()
                          if c.kind == "daily_cap"], [])

    def test_youtube_daily_cap_is_hit_quickly(self):
        for index in range(8):
            self.calendar.add(post(f"y{index}", at(2026, 5, 1, 5 + index * 2)))
        caps = [c for c in self.calendar.conflicts() if c.kind == "daily_cap"]
        self.assertEqual(len(caps), 1)
        self.assertIn("accepts 6 a day", caps[0].detail)

    def test_project_quota_spans_accounts(self):
        # Four uploads to each of two channels is eight against a six-a-day
        # project budget, and neither channel exceeds its own cap.
        for account in ("yt1", "yt2"):
            for index in range(4):
                self.calendar.add(
                    post(f"{account}-{index}", at(2026, 5, 1, 4 + index * 4),
                         account=account)
                )
        quota = [c for c in self.calendar.conflicts()
                 if c.kind == "project_quota"]
        self.assertEqual(len(quota), 1)
        self.assertIn("whole API project", quota[0].detail)

    def test_occupancy_counts_per_day_and_account(self):
        self.calendar.add(post("p1", at(2026, 5, 1, 9)))
        self.calendar.add(post("p2", at(2026, 5, 1, 15)))
        self.calendar.add(post("p3", at(2026, 5, 2, 9)))
        slots = self.calendar.occupancy(date(2026, 5, 1), date(2026, 5, 3))
        self.assertEqual(slots[(date(2026, 5, 1), "a1")].count, 2)
        self.assertEqual(slots[(date(2026, 5, 1), "a1")].remaining, 4)

    def test_cancelled_posts_do_not_occupy_a_slot(self):
        self.calendar.add(post("p1", at(2026, 5, 1, 9),
                               state=PostState.CANCELLED))
        slots = self.calendar.occupancy(date(2026, 5, 1), date(2026, 5, 1))
        self.assertEqual(slots, {})

    def test_a_draft_still_spends_the_daily_allowance(self):
        self.calendar.add(post("p1", at(2026, 5, 1, 9),
                               state=PostState.AWAITING_CREATOR))
        slots = self.calendar.occupancy(date(2026, 5, 1), date(2026, 5, 1))
        self.assertEqual(slots[(date(2026, 5, 1), "a1")].count, 1)

    def test_next_free_slot_skips_a_full_day(self):
        for index in range(6):
            self.calendar.add(post(f"p{index}", at(2026, 5, 1, 4 + index * 3)))
        slot = self.calendar.next_free_slot("a1", Platform.YOUTUBE,
                                            at(2026, 5, 1, 5))
        self.assertGreaterEqual(slot.astimezone(AMS).date(), date(2026, 5, 2))

    def test_next_free_slot_respects_spacing(self):
        self.calendar.add(post("p1", at(2026, 5, 1, 9, 0)))
        slot = self.calendar.next_free_slot("a1", Platform.YOUTUBE,
                                            at(2026, 5, 1, 9, 10))
        self.assertGreaterEqual((slot - at(2026, 5, 1, 9, 0)).total_seconds(),
                                90 * 60)

    def test_capacity_forecast_for_a_project_scoped_platform(self):
        forecast = self.calendar.capacity_forecast(
            Platform.YOUTUBE, 200, at(2026, 5, 1), accounts=["a", "b", "c"]
        )
        # Three channels do not triple a project-scoped quota.
        self.assertEqual(forecast["per_day"], 6)
        self.assertEqual(forecast["days_required"], 34)
        self.assertIn("does not help", forecast["explanation"])

    def test_capacity_forecast_scales_with_account_scoped_quota(self):
        one = self.calendar.capacity_forecast(
            Platform.INSTAGRAM, 200, at(2026, 5, 1), accounts=["a"])
        four = self.calendar.capacity_forecast(
            Platform.INSTAGRAM, 200, at(2026, 5, 1), accounts=["a", "b", "c", "d"])
        self.assertEqual(four["per_day"], one["per_day"] * 4)
        self.assertLess(four["days_required"], one["days_required"])

    def test_month_view_groups_by_local_day(self):
        self.calendar.add(post("p1", at(2026, 5, 1, 9)))
        self.calendar.add(post("p2", at(2026, 5, 1, 15)))
        view = self.calendar.month_view(2026, 5)
        self.assertEqual(view["total"], 2)
        self.assertEqual(len(view["days"]["2026-05-01"]), 2)

    def test_month_view_uses_the_calendar_timezone(self):
        # 23:30 UTC on 30 April is 01:30 on 1 May in Amsterdam.
        self.calendar.add(post("p1", at(2026, 4, 30, 23, 30)))
        self.assertIn("2026-05-01", self.calendar.month_view(2026, 5)["days"])


class TestRetryClassification(unittest.TestCase):
    NOW = at(2026, 5, 1, 12)

    def test_server_error_retries(self):
        decision = classify(Response(500), 1, Platform.YOUTUBE, self.NOW)
        self.assertIs(decision.disposition, Disposition.RETRY)
        self.assertGreater(decision.delay_s, 0)

    def test_server_error_in_flight_reconciles(self):
        # The post may already exist. Retrying would duplicate it.
        decision = classify(Response(500), 1, Platform.INSTAGRAM, self.NOW,
                            already_in_flight=True)
        self.assertIs(decision.disposition, Disposition.RECONCILE)
        self.assertTrue(decision.unsafe_to_repeat)

    def test_timeout_before_anything_was_sent_retries(self):
        decision = classify(None, 1, Platform.TIKTOK, self.NOW, timed_out=True)
        self.assertIs(decision.disposition, Disposition.RETRY)
        self.assertFalse(decision.unsafe_to_repeat)

    def test_timeout_in_flight_reconciles(self):
        decision = classify(None, 1, Platform.TIKTOK, self.NOW,
                            timed_out=True, already_in_flight=True)
        self.assertIs(decision.disposition, Disposition.RECONCILE)
        self.assertTrue(decision.unsafe_to_repeat)

    def test_conflict_reconciles(self):
        decision = classify(Response(409), 1, Platform.YOUTUBE, self.NOW)
        self.assertIs(decision.disposition, Disposition.RECONCILE)

    def test_dead_token_needs_a_human(self):
        decision = classify(
            Response(401, {}, {"error": {"code": "access_token_invalid"}}),
            1, Platform.TIKTOK, self.NOW,
        )
        self.assertIs(decision.disposition, Disposition.REAUTH)
        self.assertEqual(decision.delay_s, 0.0)

    def test_quota_reschedules_past_the_reset_rather_than_backing_off(self):
        decision = classify(
            Response(403, {}, {"error": {"errors": [{"reason": "quotaExceeded"}]}}),
            1, Platform.YOUTUBE, self.NOW,
        )
        self.assertIs(decision.disposition, Disposition.RESCHEDULE)
        # Hours, not the seconds an exponential backoff would give.
        self.assertGreater(decision.delay_s, 3600)

    def test_retry_after_header_is_honoured(self):
        decision = classify(Response(429, {"Retry-After": "900"}), 3,
                            Platform.INSTAGRAM, self.NOW)
        self.assertIs(decision.disposition, Disposition.RESCHEDULE)
        self.assertEqual(decision.delay_s, 900.0)

    def test_validation_error_is_permanent(self):
        decision = classify(
            Response(400, {}, {"error": {"code": "invalidDescription"}}),
            1, Platform.YOUTUBE, self.NOW,
        )
        self.assertIs(decision.disposition, Disposition.FAIL)
        self.assertFalse(decision.retryable)

    def test_banned_account_is_permanent(self):
        decision = classify(
            Response(400, {}, {"error": {
                "code": "spam_risk_user_banned_from_posting"}}),
            1, Platform.TIKTOK, self.NOW,
        )
        self.assertIs(decision.disposition, Disposition.FAIL)

    def test_google_nested_error_shape_is_parsed(self):
        decision = classify(
            Response(403, {}, {"error": {"errors": [{"reason": "forbidden"}]}}),
            1, Platform.YOUTUBE, self.NOW,
        )
        self.assertEqual(decision.error_code, "forbidden")

    def test_tiktok_error_shape_is_parsed(self):
        decision = classify(
            Response(400, {}, {"error": {"code": "invalid_file_upload",
                                         "message": "bad file"}}),
            1, Platform.TIKTOK, self.NOW,
        )
        self.assertEqual(decision.error_code, "invalid_file_upload")
        self.assertIn("bad file", decision.reason)

    def test_no_response_retries(self):
        decision = classify(None, 1, Platform.YOUTUBE, self.NOW)
        self.assertIs(decision.disposition, Disposition.RETRY)


class TestBackoff(unittest.TestCase):
    def test_grows_with_each_attempt(self):
        delays = [backoff_delay(n) for n in range(1, 6)]
        self.assertEqual(delays, sorted(delays))

    def test_is_capped(self):
        self.assertLessEqual(backoff_delay(50), MAX_BACKOFF_S)

    def test_jitter_is_deterministic_for_a_key(self):
        self.assertEqual(backoff_delay(3, "post-a"), backoff_delay(3, "post-a"))

    def test_different_posts_get_different_jitter(self):
        # Otherwise an outage's worth of posts all retry in lockstep.
        delays = {backoff_delay(3, f"post-{i}") for i in range(20)}
        self.assertGreater(len(delays), 15)

    def test_jitter_stays_near_the_nominal_delay(self):
        nominal = backoff_delay(4)
        for index in range(50):
            jittered = backoff_delay(4, f"post-{index}")
            self.assertGreater(jittered, nominal * 0.7)
            self.assertLess(jittered, nominal * 1.3)

    def test_exhaustion_boundary(self):
        self.assertFalse(exhausted(MAX_ATTEMPTS - 1))
        self.assertTrue(exhausted(MAX_ATTEMPTS))


if __name__ == "__main__":
    unittest.main()
