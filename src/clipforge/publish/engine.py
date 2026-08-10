"""The publishing system — orchestration.

    connect an account (OAuth)
      → schedule posts: one, in bulk, or from a recurring rule
      → validate against the platform *now*, not when the job fires
      → hold them on a calendar with conflict and capacity checks
      → a worker leases what is due, refreshes tokens, runs the state machine
      → classify failures, and retry, reschedule, reconcile or escalate

Two things shape the whole design.

**Idempotency is the product.** Every failure path asks "might this already
have posted?" before doing anything, because a duplicate on a creator's feed
costs more than a missed slot. Posts carry a derived idempotency key, workers
take a *lease* rather than a lock so a crashed worker's job becomes available
again without two workers running it at once, and any ambiguous failure goes to
reconciliation instead of retry.

**Validation happens at schedule time.** A caption three characters too long
should be rejected while the customer is looking at the screen, not three weeks
later at 6am when the job fires into a queue nobody is watching.
"""

from __future__ import annotations

import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from . import limits as limits_mod
from . import retry as retry_mod
from .adapters import Adapter, Transport, adapter_for
from .calendar import ContentCalendar, MIN_SPACING_S
from .limits import Readiness, readiness
from .oauth import ClientCredentials, InMemoryTokenStore, TokenSet, TokenStore
from .retry import Disposition
from .schedule import Recurrence
from .types import (
    Account,
    Action,
    Attempt,
    Platform,
    PostSpec,
    PostState,
    Response,
    ScheduledPost,
    Step,
    UTC,
    ensure_utc,
    utcnow,
)

#: How long a worker's claim on a post lasts. Long enough for a large upload,
#: short enough that a worker killed mid-post frees the job within the hour.
LEASE_S = 45 * 60


class ScheduleError(ValueError):
    """A post that cannot be scheduled, with every reason at once."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(problems))


@dataclass(slots=True)
class PublishResult:
    post_id: str
    state: PostState
    remote_post_id: str = ""
    attempts: int = 0
    requests: int = 0
    disposition: str = ""
    error: str = ""
    #: True when the platform accepted the file but a human must still finish
    #: the post — TikTok's unaudited draft path.
    draft: bool = False

    @property
    def published(self) -> bool:
        """Live on the platform. A draft awaiting a human is **not** this."""
        return self.state is PostState.PUBLISHED

    @property
    def delivered(self) -> bool:
        """The file reached the platform, live or awaiting a creator."""
        return self.state in (PostState.PUBLISHED, PostState.AWAITING_CREATOR)

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "state": self.state.value,
            "remote_post_id": self.remote_post_id,
            "attempts": self.attempts,
            "requests": self.requests,
            "disposition": self.disposition,
            "error": self.error,
            "draft": self.draft,
        }


@dataclass(slots=True)
class PublishConfig:
    #: Worker identity, recorded on leases so a stuck job can be traced.
    worker_id: str = "worker-1"
    lease_s: int = LEASE_S
    #: Enforce the spacing floor when scheduling. Off lets a caller place
    #: posts wherever they like and see the conflict reported instead.
    enforce_spacing: bool = True
    spacing_s: int = MIN_SPACING_S
    #: Refuse to schedule past the point where credentials stop being
    #: renewable, rather than accepting the post and failing later.
    enforce_token_horizon: bool = True
    max_attempts: int = retry_mod.MAX_ATTEMPTS


class PublishingSystem:
    """Accounts, schedules, the calendar and the worker loop."""

    def __init__(
        self,
        config: PublishConfig | None = None,
        token_store: TokenStore | None = None,
        timezone: str = "UTC",
        accounts: MutableMapping[str, Account] | None = None,
        calendar: ContentCalendar | None = None,
        series: MutableMapping[str, Recurrence] | None = None,
    ) -> None:
        """Everything durable is injected; the defaults are the volatile ones.

        Passing nothing gives an entirely in-memory system, which is what the
        tests and the demos use and what loses everything on restart. Passing
        `clipforge.store.durable.DurableTokenStore`, `DurableAccountBook` and
        `PersistentCalendar` gives the same object backed by Postgres, with no
        other change to how it is driven.

        The defaults are deliberately the volatile ones rather than a database
        the constructor conjures up: a persistence layer that appears by magic
        is one nobody notices is missing until the DSN is wrong in production.
        """

        self.config = config or PublishConfig()
        self.tokens: TokenStore = token_store or InMemoryTokenStore()
        self.accounts: MutableMapping[str, Account] = (
            {} if accounts is None else accounts
        )
        self.calendar = calendar if calendar is not None else ContentCalendar(
            tz=timezone
        )
        # The recurrence rules. Lose these and the posts already placed still
        # go out, but nothing extends the series when the horizon runs down —
        # a channel that quietly stops after ninety days with no error to
        # explain it.
        self.series: MutableMapping[str, Recurrence] = (
            {} if series is None else series
        )

    # -- accounts --------------------------------------------------------------

    def connect(self, account: Account, tokens: TokenSet | None = None) -> None:
        self.accounts[account.account_id] = account
        if tokens is not None:
            self.tokens.put(tokens)

    def readiness(self, account_id: str = "") -> list[Readiness]:
        """What each account can actually do, before anything is scheduled."""
        targets = (
            [self.accounts[account_id]] if account_id
            else list(self.accounts.values())
        )
        return [readiness(account) for account in targets]

    def automation_report(self) -> dict[str, Any]:
        """A straight answer about how automated this setup really is."""
        reports = self.readiness()
        blocked = [r for r in reports if not r.automated]
        return {
            "accounts": len(reports),
            "fully_automated": sum(1 for r in reports if r.automated),
            "blocked": [
                {"account_id": r.account_id, "platform": r.platform.value,
                 "degrades_to": r.degraded_to, "blocker": r.blocker}
                for r in blocked
            ],
            "server_side_scheduling": sorted({
                r.platform.value for r in reports if r.server_side_scheduling
            }),
            "held_locally": sorted({
                r.platform.value for r in reports if not r.server_side_scheduling
            }),
        }

    # -- scheduling -------------------------------------------------------------

    def schedule(
        self,
        account_id: str,
        spec: PostSpec,
        run_at: datetime,
        series_id: str = "",
        force: bool = False,
    ) -> ScheduledPost:
        """Book one post, validating everything now rather than later."""
        account = self._account(account_id)
        run_at = ensure_utc(run_at)

        problems = limits_mod.validate(spec, account)

        if run_at <= utcnow() and not force:
            problems.append(f"{run_at.isoformat()} is in the past")

        if self.config.enforce_token_horizon:
            tokens = self.tokens.get(account_id)
            if tokens is None:
                problems.append(f"no credentials stored for {account_id}")
            else:
                warning = tokens.horizon_warning(run_at)
                if warning and not tokens.covers(run_at):
                    problems.append(warning)

        if self.config.enforce_spacing and not force:
            clash = self._spacing_clash(account_id, run_at)
            if clash is not None:
                problems.append(
                    f"within {self.config.spacing_s // 60} minutes of "
                    f"{clash.post_id} at {clash.run_at.isoformat()}"
                )

        if problems:
            raise ScheduleError(problems)

        post = ScheduledPost(
            post_id=f"post_{uuid.uuid4().hex[:12]}",
            account_id=account_id,
            platform=account.platform,
            spec=spec,
            run_at=run_at,
            series_id=series_id,
        )
        self.calendar.add(post)
        return post

    def schedule_bulk(
        self,
        account_id: str,
        specs: Sequence[PostSpec],
        rule: Recurrence,
        start: datetime | None = None,
        horizon_days: int = 400,
    ) -> tuple[list[ScheduledPost], list[str]]:
        """Lay a batch of clips onto a recurring rule.

        Returns the posts placed and the reasons any were not. Partial success
        is the honest outcome for a bulk import: rejecting 200 clips because
        one caption is too long helps nobody, and silently dropping it helps
        less.
        """
        start = ensure_utc(start or utcnow())
        slots = rule.occurrences(start, start + timedelta(days=horizon_days))

        placed: list[ScheduledPost] = []
        rejected: list[str] = []
        series_id = rule.series_id or f"series_{uuid.uuid4().hex[:8]}"
        self.series[series_id] = rule

        slot_index = 0
        for spec in specs:
            while slot_index < len(slots):
                slot = slots[slot_index]
                slot_index += 1
                try:
                    placed.append(
                        self.schedule(account_id, spec, slot, series_id=series_id)
                    )
                    break
                except ScheduleError as error:
                    if any("within" in p or "in the past" in p
                           for p in error.problems):
                        continue        # slot unusable; try the next one
                    rejected.append(f"{spec.asset.asset_id}: {error}")
                    break
            else:
                rejected.append(
                    f"{spec.asset.asset_id}: no free slot within "
                    f"{horizon_days} days — the schedule is full"
                )

        return placed, rejected

    def schedule_series(
        self,
        account_id: str,
        spec: PostSpec,
        rule: Recurrence,
        start: datetime | None = None,
        horizon_days: int = 90,
    ) -> tuple[list[ScheduledPost], list[str]]:
        """Expand a recurring rule into concrete posts.

        Materialised rather than evaluated lazily, so the calendar shows what
        will actually happen and conflicts surface immediately. `extend_series`
        rolls the window forward.

        Returns the posts placed **and why any were not**. Swallowing the
        rejections would be the worst possible behaviour here: the most common
        reason a long-dated series comes up short is that it runs past the
        point where the account's credentials stop being renewable, and a
        caller who asked for six months of posts and silently got two needs to
        be told which of those two facts they are looking at.
        """
        start = ensure_utc(start or utcnow())
        series_id = rule.series_id or f"series_{uuid.uuid4().hex[:8]}"
        self.series[series_id] = rule

        placed: list[ScheduledPost] = []
        rejected: list[str] = []
        for slot in rule.occurrences(start, start + timedelta(days=horizon_days)):
            try:
                placed.append(
                    self.schedule(account_id, spec, slot, series_id=series_id)
                )
            except ScheduleError as error:
                rejected.append(f"{slot.isoformat()}: {error}")
        return placed, rejected

    def extend_series(
        self, series_id: str, account_id: str, spec: PostSpec,
        until: datetime,
    ) -> tuple[list[ScheduledPost], list[str]]:
        """Roll a materialised series forward to a new horizon."""
        rule = self.series.get(series_id)
        if rule is None:
            raise KeyError(f"unknown series {series_id!r}")

        existing = [p for p in self.calendar.posts if p.series_id == series_id]
        start = max((p.run_at for p in existing), default=utcnow())

        placed: list[ScheduledPost] = []
        rejected: list[str] = []
        for slot in rule.occurrences(start + timedelta(seconds=1), ensure_utc(until)):
            try:
                placed.append(
                    self.schedule(account_id, spec, slot, series_id=series_id)
                )
            except ScheduleError as error:
                rejected.append(f"{slot.isoformat()}: {error}")
        return placed, rejected

    def cancel(self, post_id: str) -> bool:
        post = self.calendar.get(post_id)
        if post is None or post.is_terminal:
            return False
        post.state = PostState.CANCELLED
        self.calendar.persist(post)
        return True

    def cancel_series(self, series_id: str) -> int:
        cancelled = 0
        touched: list[ScheduledPost] = []
        for post in self.calendar.posts:
            if post.series_id == series_id and not post.is_terminal:
                post.state = PostState.CANCELLED
                touched.append(post)
                cancelled += 1
        self.calendar.persist(*touched)
        return cancelled

    def reschedule(self, post_id: str, run_at: datetime) -> ScheduledPost:
        post = self.calendar.get(post_id)
        if post is None:
            raise KeyError(post_id)
        if post.is_terminal:
            raise ValueError(f"{post_id} is {post.state.value}")
        # Through the calendar, so the per-account index stays ordered.
        post = self.calendar.move(post_id, run_at)
        post.next_attempt_at = None
        post.state = PostState.SCHEDULED
        self.calendar.persist(post)
        return post

    # -- the worker loop ---------------------------------------------------------

    def claim(self, now: datetime | None = None, limit: int = 10
              ) -> list[ScheduledPost]:
        """Lease the posts that are due.

        A lease rather than a lock: a worker that dies mid-post does not hold
        the job forever, and a job whose lease has lapsed is re-claimable
        without two workers ever holding it at once.
        """
        now = ensure_utc(now or utcnow())
        claimed: list[ScheduledPost] = []

        for post in self.calendar.due(now):
            if post.lease_until and post.lease_until > now:
                continue
            post.lease_until = now + timedelta(seconds=self.config.lease_s)
            post.state = PostState.CLAIMED
            claimed.append(post)
            if len(claimed) >= limit:
                break
        # The lease is only a lease once it is durable. A worker that claimed
        # in memory and then died would leave the post looking unclaimed, and a
        # second worker would publish it again.
        self.calendar.persist(*claimed)
        return claimed

    def release(self, post: ScheduledPost) -> None:
        post.lease_until = None
        self.calendar.persist(post)

    def run_post(
        self,
        post: ScheduledPost,
        transport: Transport,
        now: datetime | None = None,
        max_requests: int = 400,
    ) -> PublishResult:
        """Drive one post through its platform's state machine, durably.

        The write-through is here, in a `finally` around the whole run, rather
        than at each of the twenty-odd places `_run_post` and its helpers
        mutate the post. Those are spread across four methods and a dozen early
        returns, and a persistence call added at nineteen of them is a bug
        nobody finds until a crash lands the twentieth — a post recorded as
        `uploading` for ever, or worse, one published and recorded as
        `scheduled`, which is how the same video goes out twice.

        The `finally` also covers the exception path. A transport that raises
        something unexpected still leaves the attempt on disk, which is the
        only record of what the system believed it was doing.
        """

        try:
            return self._run_post(post, transport, now, max_requests)
        finally:
            self.calendar.persist(post)

    def _run_post(
        self,
        post: ScheduledPost,
        transport: Transport,
        now: datetime | None = None,
        max_requests: int = 400,
    ) -> PublishResult:
        now = ensure_utc(now or utcnow())
        account = self._account(post.account_id)
        tokens = self.tokens.get(post.account_id)

        if tokens is None:
            return self._escalate(post, "no credentials stored", now)
        if not tokens.can_refresh(now) and tokens.is_expired(now):
            return self._escalate(
                post,
                "credentials expired and cannot be refreshed — the account "
                "must be reconnected",
                now,
            )

        adapter = adapter_for(post.platform)
        attempt = Attempt(number=post.attempt_count + 1, started_at=now)
        post.attempts.append(attempt)
        post.state = PostState.UPLOADING

        step = adapter.begin(
            post.spec, account, tokens, post.run_at, post.idempotency_key
        )
        requests = 0
        in_flight = False

        while requests < max_requests:
            if step.action is Action.DONE:
                drafted = bool(step.context.get("draft"))
                final = (
                    PostState.AWAITING_CREATOR if drafted else PostState.PUBLISHED
                )
                post.state = final
                post.remote_post_id = step.remote_post_id
                attempt.state = final
                attempt.finished_at = now
                attempt.remote_ref = step.remote_post_id
                if drafted:
                    post.last_error = (
                        "delivered to the creator's inbox as a draft — the "
                        "post is not live until a human finishes it in the app"
                    )
                self.release(post)
                return PublishResult(
                    post_id=post.post_id,
                    state=final,
                    remote_post_id=step.remote_post_id,
                    attempts=post.attempt_count,
                    requests=requests,
                    draft=drafted,
                )

            if step.action is Action.ERROR:
                return self._handle_failure(
                    post, attempt,
                    Response(status=400, body={"error": {
                        "code": step.error_code, "message": step.error_message,
                    }}),
                    now, requests, in_flight=in_flight,
                )

            if step.action is Action.WAIT:
                # Ask the adapter for its poll request rather than sleeping:
                # the caller owns the clock, so a test does not.
                poll = getattr(adapter, "poll_request", None)
                if poll is None:
                    return self._escalate(post, "adapter cannot poll", now)
                post.state = PostState.PROCESSING
                in_flight = True
                request = poll(step.context, tokens)
            else:
                request = step.request
                if request is None:
                    return self._escalate(post, "adapter produced no request", now)

            attempt.remote_ref = str(
                step.context.get("publish_id")
                or step.context.get("container_id")
                or step.context.get("session_uri")
                or attempt.remote_ref
            )

            try:
                response = transport.send(request)
            except TimeoutError:
                return self._handle_failure(
                    post, attempt, None, now, requests,
                    in_flight=in_flight, timed_out=True,
                )
            requests += 1

            if not response.ok and response.status != 308:
                return self._handle_failure(
                    post, attempt, response, now, requests, in_flight=in_flight,
                )

            # Anything past the first successful call means the platform has
            # been told to create something.
            in_flight = True
            step = adapter.advance(step.context, response)

        return self._escalate(
            post, f"exceeded {max_requests} requests without completing", now
        )

    def _handle_failure(
        self,
        post: ScheduledPost,
        attempt: Attempt,
        response: Response | None,
        now: datetime,
        requests: int,
        in_flight: bool,
        timed_out: bool = False,
    ) -> PublishResult:
        decision = retry_mod.classify(
            response, post.attempt_count, post.platform, now,
            key=post.idempotency_key, timed_out=timed_out,
            already_in_flight=in_flight,
        )

        attempt.finished_at = now
        attempt.error_code = decision.error_code
        attempt.error_message = decision.reason
        attempt.disposition = decision.disposition.value
        post.last_error = decision.reason
        self.release(post)

        if decision.disposition is Disposition.REAUTH:
            post.state = PostState.NEEDS_ATTENTION
            attempt.state = PostState.NEEDS_ATTENTION
            return PublishResult(
                post.post_id, PostState.NEEDS_ATTENTION,
                attempts=post.attempt_count, requests=requests,
                disposition=decision.disposition.value, error=decision.reason,
            )

        if decision.disposition is Disposition.FAIL:
            post.state = PostState.FAILED
            attempt.state = PostState.FAILED
            return PublishResult(
                post.post_id, PostState.FAILED,
                attempts=post.attempt_count, requests=requests,
                disposition=decision.disposition.value, error=decision.reason,
            )

        if retry_mod.exhausted(post.attempt_count):
            post.state = PostState.FAILED
            attempt.state = PostState.FAILED
            post.last_error = (
                f"gave up after {post.attempt_count} attempts: {decision.reason}"
            )
            return PublishResult(
                post.post_id, PostState.FAILED,
                attempts=post.attempt_count, requests=requests,
                disposition="exhausted", error=post.last_error,
            )

        # RETRY, RESCHEDULE and RECONCILE all come back later. The difference
        # is how far away, and — for RECONCILE — that the next pass must ask
        # the platform what exists before sending anything.
        post.state = PostState.RETRYING
        attempt.state = PostState.RETRYING
        post.next_attempt_at = now + timedelta(seconds=decision.delay_s)

        return PublishResult(
            post.post_id, PostState.RETRYING,
            attempts=post.attempt_count, requests=requests,
            disposition=decision.disposition.value, error=decision.reason,
        )

    def _escalate(
        self, post: ScheduledPost, reason: str, now: datetime
    ) -> PublishResult:
        post.state = PostState.NEEDS_ATTENTION
        post.last_error = reason
        self.release(post)
        if post.attempts:
            post.attempts[-1].finished_at = now
            post.attempts[-1].state = PostState.NEEDS_ATTENTION
            post.attempts[-1].error_message = reason
        return PublishResult(
            post.post_id, PostState.NEEDS_ATTENTION,
            attempts=post.attempt_count, error=reason,
            disposition="escalated",
        )

    def tick(
        self, transport: Transport, now: datetime | None = None,
        limit: int = 10,
    ) -> list[PublishResult]:
        """One pass of the worker loop."""
        now = ensure_utc(now or utcnow())
        return [
            self.run_post(post, transport, now)
            for post in self.claim(now, limit)
        ]

    # -- introspection -------------------------------------------------------------

    def needs_attention(self) -> list[ScheduledPost]:
        return [p for p in self.calendar.posts
                if p.state is PostState.NEEDS_ATTENTION]

    def awaiting_creator(self) -> list[ScheduledPost]:
        """Delivered but not live — someone has to finish these by hand."""
        return [p for p in self.calendar.posts
                if p.state is PostState.AWAITING_CREATOR]

    def status(self) -> dict[str, Any]:
        summary = dict(self.calendar.summary())
        summary["accounts"] = len(self.accounts)
        summary["series"] = len(self.series)
        summary["needs_attention"] = len(self.needs_attention())
        summary["awaiting_creator"] = len(self.awaiting_creator())
        summary["automation"] = self.automation_report()
        return summary

    # -- internals -------------------------------------------------------------------

    def _account(self, account_id: str) -> Account:
        account = self.accounts.get(account_id)
        if account is None:
            raise KeyError(f"unknown account {account_id!r}")
        return account

    def _spacing_clash(
        self, account_id: str, run_at: datetime
    ) -> ScheduledPost | None:
        return self.calendar.nearest(account_id, run_at, self.config.spacing_s)
