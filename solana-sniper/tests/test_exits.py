from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sniper.exits import HOLD, SELL, PositionView, evaluate_exit, sell_amount

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
D = Decimal


def pos(tp_hit=False, peak=None, opened=NOW):
    return PositionView(entry_price=D(100), peak_price=peak, tp_hit=tp_hit, opened_at=opened)


def test_hold_in_the_middle(settings):
    d = evaluate_exit(pos(), D(110), NOW, settings.exits)
    assert d.action == HOLD and d.peak_price is None  # trailing not active before TP


def test_stop_loss_at_minus_20(settings):
    assert evaluate_exit(pos(), D("80.01"), NOW, settings.exits).action == HOLD
    d = evaluate_exit(pos(), D(80), NOW, settings.exits)
    assert (d.action, d.reason, d.fraction) == (SELL, "STOP_LOSS", 1)


def test_take_profit_sells_configured_fraction_and_starts_trailing(settings):
    d = evaluate_exit(pos(), D(150), NOW, settings.exits)
    assert (d.action, d.reason, d.fraction) == (SELL, "TAKE_PROFIT", D("0.5"))
    assert d.peak_price == D(150)


def test_take_profit_fires_only_once(settings):
    d = evaluate_exit(pos(tp_hit=True, peak=D(150)), D(160), NOW, settings.exits)
    assert d.action == HOLD and d.peak_price == D(160)


def test_trailing_stop_after_tp(settings):
    p = pos(tp_hit=True, peak=D(200))
    assert evaluate_exit(p, D("170.01"), NOW, settings.exits).action == HOLD
    d = evaluate_exit(p, D(170), NOW, settings.exits)  # 200 * 0.85
    assert (d.action, d.reason, d.fraction) == (SELL, "TRAILING_STOP", 1)


def test_stop_loss_still_applies_after_tp(settings):
    d = evaluate_exit(pos(tp_hit=True, peak=D(150)), D(79), NOW, settings.exits)
    assert d.reason == "STOP_LOSS"


def test_trailing_from_entry_when_configured(settings):
    settings.exits.trailing_activate_after_tp = False
    d = evaluate_exit(pos(peak=D(130)), D(110), NOW, settings.exits)  # 130 * 0.85 = 110.5
    assert d.reason == "TRAILING_STOP"


def test_max_hold_time(settings):
    settings.exits.max_hold_minutes = 60
    old = pos(opened=NOW - timedelta(minutes=61))
    assert evaluate_exit(old, D(105), NOW, settings.exits).reason == "MAX_HOLD_TIME"


def test_sell_amount():
    assert sell_amount(1001, D("0.5")) == 500
    assert sell_amount(1001, D(1)) == 1001
