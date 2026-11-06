"""One clock for the whole system.

Records about things that happened on an earlier day (a sale from March, a
harvest from June) must carry that day's date and a sensible time of day on
every timestamp they create: order, status history, payment, invoice,
notifications. Code runs inside ``as_of(when)`` and every model whose
``created_at`` defaults to ``clock.now`` — and every service that calls
``clock.now()`` — uses that moment instead of the real time.

The audit log is the one exception: it keeps the real time, so the
administrator can always see when something was actually entered.
"""
import threading
from contextlib import contextmanager
from datetime import datetime, time, timedelta

from django.utils import timezone

_local = threading.local()


def now():
    """The current moment, or the moment set by an enclosing as_of()."""
    return getattr(_local, "at", None) or timezone.now()


def override():
    """The moment set by an enclosing as_of(), or None."""
    return getattr(_local, "at", None)


def today():
    return timezone.localtime(now()).date()


@contextmanager
def as_of(moment):
    """Run the block as if it were `moment` (None = real time)."""
    previous = getattr(_local, "at", None)
    _local.at = moment
    try:
        yield moment
    finally:
        _local.at = previous


def at_date(day, after=None):
    """A believable moment on `day`.

    Today -> the real time. An earlier day -> the current time of day if it is
    within working hours (07:00-18:00), otherwise a morning time. Never earlier
    than `after` (the previous step) and never in the future.
    """
    real = timezone.now()
    local_now = timezone.localtime(real)
    chosen = override()
    if chosen is not None and day is not None and timezone.localtime(chosen).date() == day \
            and (after is None or chosen > after):
        return chosen  # the caller already picked a moment on that day
    if day is None or day >= local_now.date():
        moment = real
    else:
        t = local_now.time()
        if not (time(7) <= t <= time(18)):
            t = time(9, local_now.minute, local_now.second)
        moment = timezone.make_aware(datetime.combine(day, t))
    if after is not None and moment <= after:
        moment = after + timedelta(minutes=25)
    return min(moment, real)
