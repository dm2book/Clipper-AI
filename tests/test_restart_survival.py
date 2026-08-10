"""Nothing important is lost after a restart.

The requirement this layer exists to satisfy, tested the only way it can
honestly be tested: in separate operating-system processes.

Asserting durability inside one process proves nothing — an in-memory dict
passes every such test right up until the deploy. So the writer here is a real
child process that is **SIGKILLed** after it commits: no shutdown hook, no
flush, no chance to write anything on the way out. Whatever the reader finds
afterwards was in Postgres already.

Skips unless `CLIPFORGE_TEST_DSN` names a migrated database, and says so.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import unittest
from datetime import UTC, datetime

_DSN = os.environ.get("CLIPFORGE_TEST_DSN", "")
_ADMIN_DSN = os.environ.get("CLIPFORGE_TEST_ADMIN_DSN", _DSN)
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

TENANT = "ten_restart"
NOW = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)

_PREAMBLE = """
import os, sys
sys.path.insert(0, {src!r})
from datetime import UTC, datetime, timedelta
from clipforge.store.postgres import PostgresDatabase
from clipforge.store import *
NOW = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)
TENANT = {tenant!r}
db = PostgresDatabase({dsn!r}, min_size=1, max_size=2)
"""


def _run(body: str, *, kill_after: bool = False) -> str:
    """Run `body` in a child process. Optionally SIGKILL it once it reports."""

    script = _PREAMBLE.format(src=_SRC, dsn=_DSN, tenant=TENANT) + textwrap.dedent(body)
    if not kill_after:
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        if done.returncode != 0:
            raise AssertionError(f"child failed:\n{done.stderr}")
        return done.stdout.strip()

    # The child prints COMMITTED and then blocks. Killing it there means the
    # process never runs another line of Python after the commit — no atexit,
    # no __del__, no pool drain.
    child = subprocess.Popen(
        [sys.executable, "-c", script + "\nprint('COMMITTED', flush=True)\nimport time\ntime.sleep(300)\n"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        line = child.stdout.readline().strip()
        if line != "COMMITTED":
            child.kill()
            raise AssertionError(f"child never committed: {line!r} {child.stderr.read()}")
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
        for pipe in (child.stdout, child.stderr):
            if pipe is not None:
                pipe.close()
    return line


@unittest.skipUnless(_DSN, "set CLIPFORGE_TEST_DSN to prove restart survival")
class RestartSurvivalTest(unittest.TestCase):
    def setUp(self) -> None:
        import psycopg

        # Even as the owner, a DELETE needs a tenant scope: migration 002
        # FORCEs row-level security, so the owner is not exempt. That is the
        # point — the likeliest way this isolation gets lost in production is
        # an application pointed at the owner role by a copy-pasted URL.
        with psycopg.connect(_ADMIN_DSN) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, false)", [TENANT]
                )
                cursor.execute("DELETE FROM tenants WHERE id = %s", [TENANT])
            connection.commit()

    def test_committed_work_outlives_a_killed_process(self) -> None:
        _run(
            """
            with db.unit_of_work(TENANT) as uow:
                uow.tenants.save(TenantRecord(id=TENANT, name="Restart Co"))
                uow.projects.save(ProjectRecord(id="proj_r", tenant_id=TENANT,
                                                name="Brand"))
                uow.channels.save(ChannelRecord(id="ch_r", tenant_id=TENANT,
                                                project_id="proj_r", name="Cars",
                                                niche="cars", state="active",
                                                consecutive_failures=4,
                                                circuit_opened_at=NOW))
                uow.accounts.save(SocialAccountRecord(id="acc_r", tenant_id=TENANT,
                                                      channel_id="ch_r",
                                                      platform="tiktok"))
                uow.clips.save(ClipRecord(id="cl_r", tenant_id=TENANT,
                                          channel_id="ch_r", hook_text="Survives",
                                          signals=["hook"], scores={"v": 0.9}))
                uow.uploads.save(UploadRecord(id="up_r", tenant_id=TENANT,
                                              channel_id="ch_r", account_id="acc_r",
                                              clip_id="cl_r", platform="tiktok",
                                              run_at=NOW,
                                              idempotency_key="acc_r:cl_r:0800"))
                uow.jobs.enqueue(JobRecord(id="job_r", tenant_id=TENANT,
                                           kind="render_video",
                                           dedupe_key="render:cl_r",
                                           payload={"clip_id": "cl_r"}))
            """,
            kill_after=True,
        )

        found = _run(
            """
            with db.unit_of_work(TENANT) as uow:
                channel = uow.channels.require("ch_r")
                clip = uow.clips.require("cl_r")
                upload = uow.uploads.require("up_r")
                job = uow.jobs.get("job_r")
                print("|".join([
                    channel.name, str(channel.consecutive_failures),
                    clip.hook_text, str(clip.scores["v"]),
                    upload.idempotency_key, upload.run_at.isoformat(),
                    job.kind, job.payload["clip_id"], job.state,
                ]))
            """
        )
        self.assertEqual(
            found,
            "Cars|4|Survives|0.9|acc_r:cl_r:0800|2026-05-01T08:00:00+00:00"
            "|render_video|cl_r|queued",
        )

    def test_uncommitted_work_does_not_survive(self) -> None:
        """The other half of the guarantee, and the one that is easy to get
        wrong: a process killed *before* its commit must leave nothing. A store
        that persisted this would be one that persists partial work."""

        _run(
            """
            uow = db.unit_of_work(TENANT)
            uow.__enter__()
            uow.tenants.save(TenantRecord(id=TENANT, name="Never Committed"))
            uow.projects.save(ProjectRecord(id="proj_ghost", tenant_id=TENANT,
                                            name="Ghost Brand"))
            uow.channels.save(ChannelRecord(id="ch_ghost", tenant_id=TENANT,
                                            project_id="proj_ghost", name="Ghost",
                                            niche="cars"))
            """,
            kill_after=True,
        )
        found = _run(
            """
            with db.unit_of_work(TENANT) as uow:
                print("GHOST" if uow.channels.get("ch_ghost") else "CLEAN")
            """
        )
        self.assertEqual(found, "CLEAN")

    def test_a_queued_job_is_still_queued_after_the_worker_dies(self) -> None:
        """The concrete version of the promise: a render that vanishes on
        restart is a clip that silently never posts. It has to be claimable
        again once the lease lapses."""

        _run(
            """
            with db.unit_of_work(TENANT) as uow:
                uow.tenants.save(TenantRecord(id=TENANT, name="Restart Co"))
                uow.jobs.enqueue(JobRecord(id="job_k", tenant_id=TENANT,
                                           kind="publish_upload", run_after=NOW))
                uow.jobs.claim("worker-that-dies", NOW, lease_s=60)
            """,
            kill_after=True,
        )
        found = _run(
            """
            from datetime import timedelta
            later = NOW + timedelta(seconds=120)
            with db.unit_of_work(TENANT) as uow:
                print(uow.jobs.get("job_k").state, uow.jobs.reap(later),
                      len(uow.jobs.claim("worker-2", later)))
            """
        )
        self.assertEqual(found, "leased 1 1")


if __name__ == "__main__":
    unittest.main()
