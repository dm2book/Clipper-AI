#!/usr/bin/env python3
"""Every authentication flow, with real bcrypt and real JWTs.

    python demo/run_auth_demo.py                  # signup to session
    python demo/run_auth_demo.py --attacks        # what the defences refuse
    python demo/run_auth_demo.py --reset          # forgotten password
    python demo/run_auth_demo.py --delete         # deletion and its grace period
    python demo/run_auth_demo.py --config         # what a deployment must set
    python demo/run_auth_demo.py --all

Nothing here is mocked: passwords go through bcrypt, tokens through PyJWT.
Storage is in memory so the demo needs no database, and the same flows run
against PostgreSQL in `tests/test_auth.py`.

**No email is sent.** The links below come from `RecordingEmailSender`, which
keeps messages instead of delivering them — the honest state of this repository,
and the reason a deployment that does not replace it registers users who never
receive a verification link.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from clipforge.auth import (  # noqa: E402
    AccessTokenIssuer,
    AuthConfig,
    AuthError,
    AuthService,
    EventKind,
    InvalidToken,
    MemoryAuthStore,
    MisconfiguredAuth,
    PasswordHasher,
    PasswordPolicy,
    RateLimited,
    RecordingEmailSender,
    describe_environment,
)

EMAIL = "dana@example.com"
PASSWORD = "marmalade tuesday bicycle"
NEW_PASSWORD = "seventeen quiet lanterns"

#: Cost 4 so the demo is instant. Production is 12, and the config guard
#: below refuses anything under 10.
FAST = PasswordPolicy(rounds=4)


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def build() -> tuple[AuthService, RecordingEmailSender, Clock]:
    store = MemoryAuthStore()
    sender = RecordingEmailSender()
    clock = Clock()
    config = AuthConfig(
        password_policy=FAST,
        verification_url="https://app.clipforge.test/verify",
        reset_url="https://app.clipforge.test/reset",
    )
    service = AuthService(
        store, AccessTokenIssuer(config.keyring, ttl_s=config.access_ttl_s),
        config=config, hasher=PasswordHasher(FAST), sender=sender, clock=clock,
    )
    return service, sender, clock


def _token(link: str) -> str:
    return link.split("token=", 1)[1]


def _rule(title: str) -> None:
    print(f"\n  ── {title} " + "─" * max(0, 58 - len(title)))


def happy_path(service: AuthService, sender: RecordingEmailSender) -> bool:
    _rule("signup to session")

    result = service.sign_up(EMAIL, PASSWORD, ip="203.0.113.7")
    print(f"\n  1. signup     {result.message}")
    identity = service.store.identity_by_email(EMAIL)
    print(f"     stored     status={identity.status.value} "
          f"hash={identity.password_hash[:29]}…")
    print(f"     algorithm  {identity.password_algo} "
          f"(real bcrypt, cost {FAST.rounds})")

    link = sender.links_for(EMAIL)[-1]
    print(f"\n  2. emailed    {link[:58]}…")
    print("     stored as  a SHA-256 hash — a database dump is not a set of "
          "working links")

    service.verify_email(_token(link))
    print("\n  3. verified   status=active")

    service.add_membership(identity.identity_id, "ten_acme", "usr_1",
                           "operator", "Acme Media")
    login = service.log_in(EMAIL, PASSWORD, ip="203.0.113.7",
                           user_agent="Firefox/132")
    print(f"\n  4. signed in  session {login.tokens.session_id}")
    print(f"     access     {login.tokens.access_token[:48]}…")
    print(f"     expires in {login.tokens.expires_in_s}s")

    principal = service.authenticate(login.tokens.access_token)
    print(f"\n  5. verified   {principal.email} as {principal.role} "
          f"in {principal.tenant_id}")

    rotated = service.refresh(login.tokens.refresh_token)
    print(f"\n  6. refreshed  new refresh token, rotated="
          f"{rotated.refresh_token != login.tokens.refresh_token}")

    service.log_out(rotated.refresh_token)
    try:
        service.refresh(rotated.refresh_token)
        print("\n  7. signed out — but the token still works. That is a bug.")
        return False
    except InvalidToken:
        print("\n  7. signed out and the refresh token is dead")
    return True


def attacks(service: AuthService, sender: RecordingEmailSender,
            clock: Clock) -> bool:
    _rule("what the defences refuse")
    ok = True

    service.sign_up(EMAIL, PASSWORD, ip="203.0.113.7")
    service.verify_email(_token(sender.links_for(EMAIL)[-1]))

    # 1. Account enumeration
    taken = service.sign_up(EMAIL, "another long enough phrase", ip="198.51.100.1")
    fresh = service.sign_up("nobody@example.com", "another long enough phrase",
                            ip="198.51.100.2")
    same = taken.message == fresh.message
    print(f"\n  enumeration    signup says the same thing either way: {same}")
    ok = ok and same

    try:
        service.log_in("ghost@example.com", "x", ip="198.51.100.3")
    except AuthError as unknown:
        try:
            service.log_in(EMAIL, "wrong", ip="198.51.100.3")
        except AuthError as wrong:
            match = unknown.code == wrong.code and unknown.message == wrong.message
            print(f"                 login says the same thing either way: {match}")
            ok = ok and match

    # 2. Refresh token theft
    login = service.log_in(EMAIL, PASSWORD, ip="203.0.113.7")
    stolen = login.tokens.refresh_token
    legitimate = service.refresh(stolen)
    try:
        service.refresh(stolen)
        print("\n  token reuse    a spent refresh token was accepted — a bug")
        ok = False
    except InvalidToken:
        print("\n  token reuse    the spent copy was refused…")
    try:
        service.refresh(legitimate.refresh_token)
        print("                 …but the live token survived — a bug")
        ok = False
    except InvalidToken:
        print("                 …and the whole family was revoked, so the "
              "thief's copy is dead")

    events = [e.kind for e in service.store.events_for()]
    print(f"                 audited: "
          f"{EventKind.TOKEN_REUSE_DETECTED in events}")

    # 3. Forged tokens
    import base64
    import json

    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT", "kid": "dev"}).encode()
    ).rstrip(b"=").decode()
    claims = base64.urlsafe_b64encode(json.dumps({
        "sub": "idn_attacker", "iss": "clipforge", "aud": "clipforge-api",
        "iat": 0, "nbf": 0, "exp": 9_999_999_999, "jti": "x", "role": "owner",
    }).encode()).rstrip(b"=").decode()
    try:
        service.authenticate(f"{header}.{claims}.")
        print("\n  alg=none       a signature-free token was accepted — a bug")
        ok = False
    except InvalidToken:
        print("\n  alg=none       refused; the algorithm is pinned, never read "
              "from the token")

    # 4. Rate limiting
    limit, _ = service.config.rate_limits["login_email"]
    blocked = False
    for _ in range(limit + 2):
        try:
            service.log_in(EMAIL, "wrong", ip="198.51.100.50")
        except RateLimited:
            blocked = True
            break
        except AuthError:
            pass
    print(f"\n  brute force    per-address limit stopped it: {blocked}")
    ok = ok and blocked

    # 5. Password never recoverable
    identity = service.store.identity_by_email(EMAIL)
    blob = repr([e.to_dict() for e in service.store.events_for()])
    clean = PASSWORD not in blob and PASSWORD not in identity.password_hash
    print(f"\n  secrets        the password appears nowhere stored or logged: "
          f"{clean}")
    return ok and clean


def reset_flow(service: AuthService, sender: RecordingEmailSender) -> bool:
    _rule("forgotten password")

    service.sign_up(EMAIL, PASSWORD, ip="203.0.113.7")
    service.verify_email(_token(sender.links_for(EMAIL)[-1]))
    session = service.log_in(EMAIL, PASSWORD, ip="203.0.113.7")
    print(f"\n  1. signed in on one device, session "
          f"{session.tokens.session_id}")

    service.request_password_reset(EMAIL, ip="203.0.113.7")
    token = _token(sender.links_for(EMAIL)[-1])
    print("  2. reset link issued; any earlier link is now dead")

    service.reset_password(token, NEW_PASSWORD, ip="203.0.113.7")
    print("  3. password changed")

    try:
        service.refresh(session.tokens.refresh_token)
        print("  4. the old session still works — that is a bug")
        return False
    except InvalidToken:
        print("  4. every existing session was revoked, so a thief's token "
              "dies with the reset")

    try:
        service.reset_password(token, "yet another long phrase")
        print("  5. the link worked twice — that is a bug")
        return False
    except AuthError:
        print("  5. the link is single use")

    notice = sender.last_to(EMAIL)
    print(f"  6. owner notified: {notice.kind}")
    return service.log_in(EMAIL, NEW_PASSWORD).tokens.access_token != ""


def deletion(service: AuthService, sender: RecordingEmailSender,
             clock: Clock) -> bool:
    _rule("deletion, with a way back")

    service.sign_up(EMAIL, PASSWORD, ip="203.0.113.7")
    service.verify_email(_token(sender.links_for(EMAIL)[-1]))
    identity = service.store.identity_by_email(EMAIL)
    service.add_membership(identity.identity_id, "ten_acme", "usr_1", "owner")

    service.request_deletion(identity.identity_id, ip="203.0.113.7")
    stored = service.store.identity(identity.identity_id)
    print(f"\n  1. requested   status={stored.status.value}, "
          f"purge due {stored.delete_after:%d %b %Y}")
    print("     signed out immediately, but nothing is gone yet")

    print(f"  2. purge now?  {service.purge_due(clock()) or 'nothing due'}")

    service.log_in(EMAIL, PASSWORD, ip="203.0.113.7")
    print(f"  3. signed in   deletion cancelled: "
          f"{service.store.identity(identity.identity_id).status.value}")

    service.request_deletion(identity.identity_id)
    clock.advance(seconds=service.config.deletion_grace_s + 1)
    purged = service.purge_due(clock())
    print(f"\n  4. grace over  purged {purged}")

    after = service.store.identity(identity.identity_id)
    print(f"     row kept    status={after.status.value}, "
          f"email={after.email}")
    print(f"     hash gone   {after.password_hash == ''}")
    print(f"     memberships {len(service.store.memberships(identity.identity_id))}")
    print("     the row survives so the audit trail can still answer "
          "'who did this'")

    freed = service.sign_up(EMAIL, NEW_PASSWORD)
    print(f"  5. address free again: {freed.created}")
    return after.password_hash == "" and freed.created


def configuration() -> bool:
    _rule("what a deployment must set")

    report = describe_environment()
    print(f"\n  signing keys   {report['signing_keys']} "
          f"({report['signing_key_source']})")
    print(f"  access ttl     {report['access_ttl_s']}s")
    print(f"  refresh ttl    {report['refresh_ttl_s']}s "
          f"(ceiling {report['session_max_s']}s)")
    print(f"  bcrypt cost    {report['bcrypt_rounds']}")
    print(f"  ready          {report['production_ready']}")
    for problem in report["problems"]:
        print(f"    ✗ {problem}")

    print("\n  The guard refuses the defaults on purpose. A generated signing")
    print("  key works perfectly, signs everyone out on every restart, and")
    print("  cannot be verified by a second instance.")

    try:
        AuthConfig().require_production_ready()
        print("\n  the default config passed the guard — that is a bug")
        return False
    except MisconfiguredAuth:
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attacks", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--config", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    chose = args.attacks or args.reset or args.delete or args.config
    print("\n  Real bcrypt, real JWTs, in-memory storage, no email sent.")

    ok = True
    if args.all or not chose:
        service, sender, _ = build()
        ok = happy_path(service, sender) and ok
    if args.all or args.attacks:
        service, sender, clock = build()
        ok = attacks(service, sender, clock) and ok
    if args.all or args.reset:
        service, sender, _ = build()
        ok = reset_flow(service, sender) and ok
    if args.all or args.delete:
        service, sender, clock = build()
        ok = deletion(service, sender, clock) and ok
    if args.all or args.config:
        ok = configuration() and ok

    print("\n  " + ("all good\n" if ok else "something failed above\n"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
