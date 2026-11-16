#
# smpplib -- SMPP Library for Python
# Copyright (c) 2005 Martynas Jocius <mjoc@akl.lt>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
Offline notification dispatch planning and verification.

This module performs no network IO.  It consumes a timestamped event log of
what happened to a notification (enqueued, submit attempts and their outcomes,
reconciliation decisions) and produces the decision timeline that the same
dispatch rules would generate online.  It therefore allows a dispatch plan or
an observed send log to be verified offline.

The central safety rule encoded here is the distinction between:

* an *explicit* SMSC response -- either an acceptance (ROK) or a definite
  rejection / throttle, and
* a *missing* response (read timeout, socket reset, disconnect before the
  response arrived), whose outcome is UNKNOWN: the SMSC may already have
  accepted the message, so it must never be resent automatically.  Resending
  is only allowed after reconciliation (query_sm or operator confirmation)
  proves the SMSC did not accept it.

Retries of throttled submissions keep the same ``BusinessIdentity``; a new
transport sequence does not mean a new business message.
"""

from smpplib import consts


# Attempt dispositions.
DISPOSITION_ACCEPTED = 'accepted'
DISPOSITION_REJECTED = 'rejected'
DISPOSITION_RETRYABLE = 'retryable'
DISPOSITION_UNKNOWN = 'unknown'

# Job states.
STATE_QUEUED = 'queued'
STATE_SCHEDULED = 'scheduled'
STATE_SUCCEEDED = 'succeeded'
STATE_PERMANENTLY_REJECTED = 'permanently_rejected'
STATE_RETRY_EXHAUSTED = 'retry_exhausted'
STATE_AWAITING_RECONCILIATION = 'awaiting_reconciliation'
STATE_WINDOW_CLOSED = 'window_closed'

TERMINAL_STATES = frozenset((
    STATE_SUCCEEDED,
    STATE_PERMANENTLY_REJECTED,
    STATE_RETRY_EXHAUSTED,
    STATE_WINDOW_CLOSED,
))

# Actions emitted on the decision timeline.
ACTION_DISPATCH = 'dispatch'
ACTION_RETRY = 'retry'
ACTION_SUCCEEDED = 'succeeded'
ACTION_REJECTED = 'rejected'
ACTION_HOLD_RECONCILIATION = 'hold_reconciliation'
ACTION_GIVE_UP = 'give_up'

# Policy violations that can be found in an observed event log.
VIOLATION_DUPLICATE_ENQUEUE = 'duplicate_enqueue'
VIOLATION_UNKNOWN_JOB = 'unknown_job'
VIOLATION_IDENTITY_DRIFT = 'business_identity_drift'
VIOLATION_AFTER_TERMINAL = 'dispatch_after_terminal_state'
VIOLATION_UNSAFE_RESEND = 'unsafe_resend_after_unknown_outcome'
VIOLATION_BEFORE_SCHEDULE = 'dispatch_before_scheduled_time'
VIOLATION_RATE_LIMIT = 'rate_limit_exceeded'
VIOLATION_OUTSIDE_WINDOW = 'dispatch_outside_window'
VIOLATION_INVALID_RECONCILE = 'reconcile_without_unknown_outcome'

# Explicit statuses that warrant an automatic retry.  Everything else that is
# an explicit non-OK response is treated as a definite rejection.
DEFAULT_RETRYABLE_STATUSES = frozenset((
    consts.SMPP_ESME_RTHROTTLED,   # Throttling, exceeded message limits
    consts.SMPP_ESME_RMSGQFUL,      # Message queue full
    consts.SMPP_ESME_RSYSERR,       # Transient SMSC system error
    consts.SMPP_ESME_RX_T_APPN,     # Temporary application error
))


class AttemptOutcome(object):
    """Classification of one submit attempt."""

    __slots__ = ('disposition', 'status', 'retry_safe', 'description')

    def __init__(self, disposition, status, description):
        self.disposition = disposition
        self.status = status
        self.retry_safe = disposition == DISPOSITION_RETRYABLE
        self.description = description

    def __repr__(self):
        return 'AttemptOutcome({}, status={})'.format(self.disposition, self.status)


def classify_status(status, retryable_statuses=DEFAULT_RETRYABLE_STATUSES):
    """
    Classify an explicit SMPP command status, or the absence of one.

    ``None`` means no response was received at all (timeout / disconnect);
    the outcome is unknown and an automatic resend is not safe.
    """
    if status is None:
        return AttemptOutcome(
            DISPOSITION_UNKNOWN,
            None,
            'No response received; the SMSC outcome is unknown',
        )
    if status == consts.SMPP_ESME_ROK:
        return AttemptOutcome(
            DISPOSITION_ACCEPTED,
            status,
            consts.DESCRIPTIONS.get(status, 'Accepted'),
        )
    if status in retryable_statuses:
        return AttemptOutcome(
            DISPOSITION_RETRYABLE,
            status,
            consts.DESCRIPTIONS.get(status, 'Transient failure'),
        )
    return AttemptOutcome(
        DISPOSITION_REJECTED,
        status,
        consts.DESCRIPTIONS.get(status, 'Explicit rejection'),
    )


def connection_interrupted():
    """
    Outcome when the connection breaks or times out before a response arrives.

    The SMSC may or may not have processed the submit, so the result is
    unknown and must be reconciled rather than blindly resent.
    """
    return AttemptOutcome(
        DISPOSITION_UNKNOWN,
        None,
        'Connection interrupted before a response was received',
    )


class BusinessIdentity(object):
    """
    Stable business identity of a notification, independent of transport.

    ``reference`` is the persistent business key (e.g. a notification UUID).
    Throttled retries reuse the same identity even though each transport
    attempt gets its own SMPP sequence number.
    """

    __slots__ = ('reference', 'detail')

    def __init__(self, reference, **detail):
        self.reference = str(reference)
        self.detail = dict(detail)

    @property
    def key(self):
        return self.reference

    def __eq__(self, other):
        return isinstance(other, BusinessIdentity) and other.reference == self.reference

    def __hash__(self):
        return hash(self.reference)

    def __repr__(self):
        return 'BusinessIdentity({!r})'.format(self.reference)


class BackoffPolicy(object):
    """Capped exponential backoff between retries."""

    def __init__(self, initial_delay=1.0, multiplier=2.0, max_delay=60.0,
                 max_attempts=5, jitter=None):
        if initial_delay <= 0:
            raise ValueError('initial_delay must be positive')
        if multiplier < 1:
            raise ValueError('multiplier must be >= 1')
        if max_delay < initial_delay:
            raise ValueError('max_delay must be >= initial_delay')
        if max_attempts < 1:
            raise ValueError('max_attempts must be >= 1')
        self.initial_delay = float(initial_delay)
        self.multiplier = float(multiplier)
        self.max_delay = float(max_delay)
        self.max_attempts = max_attempts
        # Optional callable (delay, retry_number) -> delay for deterministic
        # tests or pluggable jitter.  Defaults to no jitter so offline
        # timelines are reproducible.
        self.jitter = jitter

    def delay_for(self, retry_number):
        """Delay before the given 1-based retry (1 is the first retry)."""
        if retry_number < 1:
            raise ValueError('retry_number is 1-based')
        delay = min(
            self.initial_delay * (self.multiplier ** (retry_number - 1)),
            self.max_delay,
        )
        if self.jitter is not None:
            delay = self.jitter(delay, retry_number)
        return delay

    def retry_allowed(self, attempts_made):
        """True if another attempt may be made after ``attempts_made``."""
        return attempts_made < self.max_attempts


class Window(object):
    """Half-open time interval [start, end) on the simulation clock."""

    __slots__ = ('start', 'end')

    def __init__(self, start, end):
        if not start < end:
            raise ValueError('window start must be before end')
        self.start = start
        self.end = end

    def contains(self, at):
        return self.start <= at < self.end

    def __repr__(self):
        return 'Window({}, {})'.format(self.start, self.end)


class SendWindow(object):
    """Set of time intervals during which dispatching is permitted."""

    def __init__(self, windows):
        merged = []
        for window in sorted(windows, key=lambda item: item.start):
            if merged and window.start <= merged[-1].end:
                merged[-1] = Window(merged[-1].start, max(merged[-1].end, window.end))
            else:
                merged.append(Window(window.start, window.end))
        self.windows = tuple(merged)

    @classmethod
    def always_open(cls):
        return cls((Window(0, float('inf')),))

    def is_open(self, at):
        for window in self.windows:
            if window.contains(at):
                return True
        return False

    def next_open(self, at):
        """
        Earliest time >= ``at`` at which dispatch is allowed.

        Returns ``at`` when a window is already open, the next window's start
        when currently closed, or ``None`` when no later window exists.
        """
        for window in self.windows:
            if at < window.start:
                return window.start
            if window.contains(at):
                return at
        return None


class RateLimiter(object):
    """Sliding-window rate limiter for client-side submit throttling."""

    def __init__(self, limit, interval):
        if limit is not None and limit < 1:
            raise ValueError('limit must be >= 1 or None')
        self.limit = limit
        self.interval = float(interval)
        self._uses = []

    @classmethod
    def unlimited(cls):
        return cls(None, 0.0)

    def _prune(self, at):
        self._uses = [used_at for used_at in self._uses if used_at > at - self.interval]

    def try_acquire(self, at):
        """Consume a slot if one is available at ``at``."""
        if self.limit is None:
            self._uses.append(at)
            return True
        self._prune(at)
        if len(self._uses) < self.limit:
            self._uses.append(at)
            return True
        return False

    def next_available(self, at):
        """Earliest time >= ``at`` at which a slot is free."""
        if self.limit is None:
            return at
        self._prune(at)
        if len(self._uses) < self.limit:
            return at
        return self._uses[0] + self.interval


class AttemptRecord(object):
    """One observed transport attempt for a job."""

    __slots__ = ('at', 'sequence', 'outcome')

    def __init__(self, at, outcome, sequence=None):
        self.at = at
        self.sequence = sequence
        self.outcome = outcome


class NotificationJob(object):
    """Mutable state of one notification while it is being dispatched."""

    def __init__(self, identity, enqueued_at):
        self.identity = identity
        self.enqueued_at = enqueued_at
        self.attempts = []
        self.state = STATE_QUEUED
        self.scheduled_at = None
        self.reason = None

    @property
    def attempts_made(self):
        return len(self.attempts)


class Action(object):
    """A decision emitted by the planner."""

    __slots__ = ('kind', 'at', 'scheduled_at', 'reason')

    def __init__(self, kind, at, reason, scheduled_at=None):
        self.kind = kind
        self.at = at
        self.scheduled_at = scheduled_at
        self.reason = reason

    def __repr__(self):
        return 'Action({}, at={})'.format(self.kind, self.at)


class RetryPlanner(object):
    """
    Stateless dispatch rules; all mutable state lives in NotificationJob.

    The planner only schedules work when it is permitted: the job must not be
    terminal or awaiting reconciliation, a send window must be open, and a
    rate-limit slot must be available.
    """

    def __init__(self, backoff=None, send_window=None, rate_limiter=None):
        self.backoff = backoff or BackoffPolicy()
        self.send_window = send_window or SendWindow.always_open()
        self.rate_limiter = rate_limiter or RateLimiter.unlimited()

    def first_dispatch_at(self, at):
        """Earliest permitted time for the initial submit."""
        return self._earliest_permitted(at)

    def record_attempt(self, job, at, outcome, sequence=None):
        """Apply an observed attempt outcome and return the next action."""
        job.attempts.append(AttemptRecord(at, outcome, sequence))
        disposition = outcome.disposition

        if disposition == DISPOSITION_ACCEPTED:
            job.state = STATE_SUCCEEDED
            job.scheduled_at = None
            job.reason = outcome.description
            return Action(ACTION_SUCCEEDED, at, outcome.description)

        if disposition == DISPOSITION_REJECTED:
            job.state = STATE_PERMANENTLY_REJECTED
            job.scheduled_at = None
            job.reason = outcome.description
            return Action(ACTION_REJECTED, at, outcome.description)

        if disposition == DISPOSITION_UNKNOWN:
            job.state = STATE_AWAITING_RECONCILIATION
            job.scheduled_at = None
            job.reason = outcome.description
            return Action(ACTION_HOLD_RECONCILIATION, at, outcome.description)

        return self._schedule_retry(job, at, outcome.description)

    def reconcile(self, job, at, accepted):
        """
        Resolve a job held after an unknown outcome.

        ``accepted=True`` means the SMSC did accept the original submit.
        ``accepted=False`` is an explicit confirmation that it did not, which
        is the only situation in which resending a previously unanswered
        message is allowed.
        """
        if job.state != STATE_AWAITING_RECONCILIATION:
            raise ValueError('job is not awaiting reconciliation')
        if accepted:
            job.state = STATE_SUCCEEDED
            job.reason = 'Reconciled: SMSC accepted the original submit'
            return Action(ACTION_SUCCEEDED, at, job.reason)
        return self._schedule_retry(
            job, at, 'Reconciled: SMSC did not accept; resend permitted')

    def _schedule_retry(self, job, at, reason):
        if not self.backoff.retry_allowed(job.attempts_made):
            job.state = STATE_RETRY_EXHAUSTED
            job.scheduled_at = None
            job.reason = 'Retry budget exhausted after {} attempt(s)'.format(job.attempts_made)
            return Action(ACTION_GIVE_UP, at, job.reason)

        retry_number = job.attempts_made
        earliest = at + self.backoff.delay_for(retry_number)
        scheduled_at = self._earliest_permitted(earliest)
        if scheduled_at is None:
            job.state = STATE_WINDOW_CLOSED
            job.scheduled_at = None
            job.reason = 'No open send window remains for the retry'
            return Action(ACTION_GIVE_UP, at, job.reason)

        job.state = STATE_SCHEDULED
        job.scheduled_at = scheduled_at
        return Action(ACTION_RETRY, at, reason, scheduled_at=scheduled_at)

    def _earliest_permitted(self, earliest_at):
        """Push a candidate time through the send window and rate limiter."""
        candidate = earliest_at
        for _ in range(len(self.send_window.windows) + 1):
            windowed = self.send_window.next_open(candidate)
            if windowed is None:
                return None
            candidate = self.rate_limiter.next_available(windowed)
            if self.send_window.is_open(candidate):
                return candidate
        return candidate


class Violation(object):
    """A rule violation detected in an observed event log."""

    __slots__ = ('at', 'identity', 'code', 'detail')

    def __init__(self, at, identity, code, detail):
        self.at = at
        self.identity = identity
        self.code = code
        self.detail = detail

    def __repr__(self):
        return 'Violation({}, {}, {})'.format(self.code, self.at, self.identity)


class TimelineEntry(object):
    """One entry of the offline decision timeline."""

    __slots__ = ('at', 'identity', 'action', 'scheduled_at', 'reason')

    def __init__(self, at, identity, action, reason, scheduled_at=None):
        self.at = at
        self.identity = identity
        self.action = action
        self.scheduled_at = scheduled_at
        self.reason = reason

    def __repr__(self):
        return 'TimelineEntry({}, {}, {})'.format(self.at, self.identity, self.action)


class Event(object):
    """An observed fact on the simulation clock."""

    ENQUEUE = 'enqueue'
    ATTEMPT = 'attempt'
    RECONCILE = 'reconcile'

    __slots__ = ('at', 'identity', 'kind', 'status', 'interrupted',
                 'accepted', 'sequence', 'reference')

    def __init__(self, at, identity, kind, status=None, interrupted=False,
                 accepted=None, sequence=None, reference=None):
        self.at = at
        self.identity = identity
        self.kind = kind
        self.status = status
        self.interrupted = interrupted
        self.accepted = accepted
        self.sequence = sequence
        self.reference = reference

    @classmethod
    def enqueue(cls, at, identity):
        return cls(at, identity, cls.ENQUEUE)

    @classmethod
    def attempt(cls, at, identity, status=None, interrupted=False,
                sequence=None, reference=None):
        return cls(at, identity, cls.ATTEMPT, status=status,
                   interrupted=interrupted, sequence=sequence,
                   reference=reference)

    @classmethod
    def reconcile(cls, at, identity, accepted):
        return cls(at, identity, cls.RECONCILE, accepted=accepted)


class SimulationResult(object):
    """Final jobs, decision timeline and violations found in a run."""

    def __init__(self, jobs, timeline, violations):
        self.jobs = jobs
        self.timeline = timeline
        self.violations = violations

    @property
    def ok(self):
        return not self.violations

    def state_of(self, identity):
        key = identity.key if isinstance(identity, BusinessIdentity) else str(identity)
        return self.jobs[key].state


def simulate(events, planner=None):
    """
    Replay an event log offline and return the full decision timeline.

    Events are processed in chronological order.  Besides producing the
    planner's decisions, the replay flags log entries that break the dispatch
    rules, notably resending a message whose outcome is still unknown.
    """
    planner = planner or RetryPlanner()
    jobs = {}
    timeline = []
    violations = []

    for event in sorted(events, key=lambda item: item.at):
        identity = event.identity
        key = identity.key

        if event.kind == Event.ENQUEUE:
            if key in jobs:
                violations.append(Violation(
                    event.at, identity, VIOLATION_DUPLICATE_ENQUEUE,
                    'notification was already enqueued'))
                continue
            job = NotificationJob(identity, event.at)
            jobs[key] = job
            scheduled_at = planner.first_dispatch_at(event.at)
            if scheduled_at is None:
                job.state = STATE_WINDOW_CLOSED
                job.reason = 'No open send window for the initial submit'
                timeline.append(TimelineEntry(
                    event.at, identity, ACTION_GIVE_UP, job.reason))
            else:
                job.state = STATE_SCHEDULED
                job.scheduled_at = scheduled_at
                timeline.append(TimelineEntry(
                    event.at, identity, ACTION_DISPATCH,
                    'Initial submit scheduled', scheduled_at=scheduled_at))
            continue

        job = jobs.get(key)
        if job is None:
            violations.append(Violation(
                event.at, identity, VIOLATION_UNKNOWN_JOB,
                'event for a notification that was never enqueued'))
            continue

        if event.kind == Event.RECONCILE:
            if job.state != STATE_AWAITING_RECONCILIATION:
                violations.append(Violation(
                    event.at, identity, VIOLATION_INVALID_RECONCILE,
                    'reconciliation offered while state is {}'.format(job.state)))
                continue
            action = planner.reconcile(job, event.at, event.accepted)
            timeline.append(TimelineEntry(
                action.at, identity, action.kind, action.reason,
                scheduled_at=action.scheduled_at))
            continue

        if event.reference is not None and event.reference != identity.reference:
            violations.append(Violation(
                event.at, identity, VIOLATION_IDENTITY_DRIFT,
                'attempt carried reference {!r}, expected {!r}'.format(
                    event.reference, identity.reference)))

        if job.state in TERMINAL_STATES:
            violations.append(Violation(
                event.at, identity, VIOLATION_AFTER_TERMINAL,
                'attempt after terminal state {}'.format(job.state)))
        elif job.state == STATE_AWAITING_RECONCILIATION:
            violations.append(Violation(
                event.at, identity, VIOLATION_UNSAFE_RESEND,
                'resend while the SMSC outcome is still unknown; '
                'reconciliation is required first'))

        if job.scheduled_at is not None and event.at < job.scheduled_at:
            violations.append(Violation(
                event.at, identity, VIOLATION_BEFORE_SCHEDULE,
                'attempt at {} before scheduled time {}'.format(
                    event.at, job.scheduled_at)))

        if not planner.send_window.is_open(event.at):
            violations.append(Violation(
                event.at, identity, VIOLATION_OUTSIDE_WINDOW,
                'attempt while every send window is closed'))

        if not planner.rate_limiter.try_acquire(event.at):
            violations.append(Violation(
                event.at, identity, VIOLATION_RATE_LIMIT,
                'rate limit exceeded at {}'.format(event.at)))

        if event.interrupted:
            outcome = connection_interrupted()
        else:
            outcome = classify_status(event.status)

        action = planner.record_attempt(job, event.at, outcome, event.sequence)
        timeline.append(TimelineEntry(
            action.at, identity, action.kind, action.reason,
            scheduled_at=action.scheduled_at))

    return SimulationResult(jobs, timeline, violations)


def verify(events, backoff=None, send_window=None, rate_limiter=None):
    """Convenience entry point: build a planner and replay ``events``."""
    planner = RetryPlanner(
        backoff=backoff,
        send_window=send_window,
        rate_limiter=rate_limiter,
    )
    return simulate(events, planner=planner)
