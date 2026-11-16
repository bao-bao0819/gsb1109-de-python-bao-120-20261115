import pytest

from smpplib import consts
from smpplib import retry


def make_identity(reference='notif-1'):
    return retry.BusinessIdentity(reference)


def test_classify_explicit_accepted():
    outcome = retry.classify_status(consts.SMPP_ESME_ROK)
    assert outcome.disposition == retry.DISPOSITION_ACCEPTED
    assert outcome.retry_safe is False


def test_classify_throttled_is_retryable():
    outcome = retry.classify_status(consts.SMPP_ESME_RTHROTTLED)
    assert outcome.disposition == retry.DISPOSITION_RETRYABLE
    assert outcome.retry_safe is True


def test_classify_explicit_rejection_is_not_retried():
    outcome = retry.classify_status(consts.SMPP_ESME_RINVDSTADR)
    assert outcome.disposition == retry.DISPOSITION_REJECTED
    assert outcome.retry_safe is False


def test_missing_response_is_unknown_and_never_retry_safe():
    assert retry.classify_status(None).disposition == retry.DISPOSITION_UNKNOWN
    interrupted = retry.connection_interrupted()
    assert interrupted.disposition == retry.DISPOSITION_UNKNOWN
    assert interrupted.retry_safe is False


def test_backoff_is_exponential_and_capped():
    backoff = retry.BackoffPolicy(
        initial_delay=2, multiplier=3, max_delay=20, max_attempts=4)
    assert backoff.delay_for(1) == 2
    assert backoff.delay_for(2) == 6
    assert backoff.delay_for(3) == 18
    assert backoff.delay_for(4) == 20  # capped
    assert backoff.retry_allowed(3) is True
    assert backoff.retry_allowed(4) is False


def test_backoff_rejects_bad_configuration():
    with pytest.raises(ValueError):
        retry.BackoffPolicy(initial_delay=0)
    with pytest.raises(ValueError):
        retry.BackoffPolicy(multiplier=0.5)
    with pytest.raises(ValueError):
        retry.BackoffPolicy(max_attempts=0)


def test_send_window_membership_and_next_open():
    window = retry.SendWindow((retry.Window(10, 20), retry.Window(30, 40)))
    assert window.is_open(10)
    assert window.is_open(19)
    assert not window.is_open(20)
    assert not window.is_open(25)
    assert window.next_open(5) == 10
    assert window.next_open(25) == 30
    assert window.next_open(40) is None


def test_overlapping_windows_are_merged():
    window = retry.SendWindow((retry.Window(0, 10), retry.Window(5, 15)))
    assert len(window.windows) == 1
    assert window.is_open(12)


def test_rate_limiter_sliding_window():
    limiter = retry.RateLimiter(limit=2, interval=10)
    assert limiter.try_acquire(0)
    assert limiter.try_acquire(5)
    assert not limiter.try_acquire(6)
    # The slot taken at t=0 frees at t=10.
    assert limiter.next_available(6) == 10
    assert limiter.try_acquire(10)


def test_happy_path_accepts_on_first_attempt():
    identity = make_identity()
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, status=consts.SMPP_ESME_ROK, sequence=100),
    ]
    result = retry.verify(events)
    assert result.ok
    assert result.state_of(identity) == retry.STATE_SUCCEEDED
    kinds = [entry.action for entry in result.timeline]
    assert kinds == [retry.ACTION_DISPATCH, retry.ACTION_SUCCEEDED]


def test_throttled_retries_keep_same_business_identity():
    identity = make_identity('notif-throttle')
    planner = retry.RetryPlanner(
        backoff=retry.BackoffPolicy(initial_delay=1, multiplier=2, max_attempts=3))
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, status=consts.SMPP_ESME_RTHROTTLED, sequence=1),
        retry.Event.attempt(2, identity, status=consts.SMPP_ESME_RTHROTTLED,
                            sequence=2, reference='notif-throttle'),
        retry.Event.attempt(4, identity, status=consts.SMPP_ESME_ROK, sequence=3),
    ]
    result = retry.simulate(events, planner)
    assert result.ok
    assert result.state_of(identity) == retry.STATE_SUCCEEDED
    job = result.jobs[identity.key]
    assert len(job.attempts) == 3
    assert all(record.outcome is not None for record in job.attempts)
    retry_action = result.timeline[1]
    assert retry_action.action == retry.ACTION_RETRY
    assert retry_action.scheduled_at == 2
    assert result.timeline[2].scheduled_at == 4
    # Every attempt still belongs to the single business identity.
    assert {entry.identity.key for entry in result.timeline} == {'notif-throttle'}


def test_retry_budget_exhaustion_is_terminal():
    identity = make_identity()
    planner = retry.RetryPlanner(
        backoff=retry.BackoffPolicy(initial_delay=1, max_attempts=2))
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, status=consts.SMPP_ESME_RTHROTTLED),
        retry.Event.attempt(2, identity, status=consts.SMPP_ESME_RTHROTTLED),
    ]
    result = retry.simulate(events, planner)
    assert result.state_of(identity) == retry.STATE_RETRY_EXHAUSTED
    assert result.timeline[-1].action == retry.ACTION_GIVE_UP


def test_explicit_rejection_needs_no_retry():
    identity = make_identity()
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, status=consts.SMPP_ESME_RINVDSTADR),
    ]
    result = retry.verify(events)
    assert result.ok
    assert result.state_of(identity) == retry.STATE_PERMANENTLY_REJECTED
    assert result.timeline[-1].action == retry.ACTION_REJECTED


def test_interrupted_connection_holds_for_reconciliation():
    identity = make_identity()
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, interrupted=True, sequence=7),
    ]
    result = retry.verify(events)
    assert result.state_of(identity) == retry.STATE_AWAITING_RECONCILIATION
    assert result.timeline[-1].action == retry.ACTION_HOLD_RECONCILIATION


def test_resending_without_response_is_flagged_unsafe():
    identity = make_identity()
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, interrupted=True, sequence=7),
        # No reconciliation: treating "no response" as "safe to resend".
        retry.Event.attempt(2, identity, status=consts.SMPP_ESME_ROK, sequence=8),
    ]
    result = retry.verify(events)
    codes = {violation.code for violation in result.violations}
    assert retry.VIOLATION_UNSAFE_RESEND in codes
    assert not result.ok


def test_reconcile_accepted_completes_without_resend():
    identity = make_identity()
    planner = retry.RetryPlanner()
    job = retry.NotificationJob(identity, enqueued_at=0)
    planner.record_attempt(job, 1, retry.connection_interrupted())
    assert job.state == retry.STATE_AWAITING_RECONCILIATION
    action = planner.reconcile(job, 5, accepted=True)
    assert action.kind == retry.ACTION_SUCCEEDED
    assert job.state == retry.STATE_SUCCEEDED
    assert len(job.attempts) == 1


def test_reconcile_rejected_allows_one_retry_with_same_identity():
    identity = make_identity()
    planner = retry.RetryPlanner(
        backoff=retry.BackoffPolicy(initial_delay=4, max_attempts=3))
    job = retry.NotificationJob(identity, enqueued_at=0)
    planner.record_attempt(job, 1, retry.connection_interrupted())
    action = planner.reconcile(job, 5, accepted=False)
    assert action.kind == retry.ACTION_RETRY
    assert action.scheduled_at == 9
    assert job.state == retry.STATE_SCHEDULED
    assert job.identity is identity


def test_reconcile_requires_unknown_outcome_state():
    identity = make_identity()
    planner = retry.RetryPlanner()
    job = retry.NotificationJob(identity, enqueued_at=0)
    with pytest.raises(ValueError):
        planner.reconcile(job, 1, accepted=False)


def test_retry_is_deferred_until_window_reopens():
    identity = make_identity()
    planner = retry.RetryPlanner(
        backoff=retry.BackoffPolicy(initial_delay=1, max_attempts=3),
        send_window=retry.SendWindow((retry.Window(0, 10), retry.Window(20, 30))))
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(9, identity, status=consts.SMPP_ESME_RTHROTTLED),
    ]
    result = retry.simulate(events, planner)
    retry_action = result.timeline[-1]
    # Backoff would suggest t=10, but the first window closes at t=10 and the
    # next one only opens at t=20.
    assert retry_action.scheduled_at == 20


def test_dispatch_outside_window_is_a_violation():
    identity = make_identity()
    planner = retry.RetryPlanner(
        send_window=retry.SendWindow((retry.Window(0, 10),)))
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(15, identity, status=consts.SMPP_ESME_ROK),
    ]
    result = retry.simulate(events, planner)
    assert retry.VIOLATION_OUTSIDE_WINDOW in {v.code for v in result.violations}


def test_rate_limit_violation_across_jobs():
    first = retry.BusinessIdentity('a')
    second = retry.BusinessIdentity('b')
    planner = retry.RetryPlanner(
        rate_limiter=retry.RateLimiter(limit=1, interval=10))
    events = [
        retry.Event.enqueue(0, first),
        retry.Event.enqueue(0, second),
        retry.Event.attempt(1, first, status=consts.SMPP_ESME_ROK),
        retry.Event.attempt(2, second, status=consts.SMPP_ESME_ROK),
    ]
    result = retry.simulate(events, planner)
    assert any(v.code == retry.VIOLATION_RATE_LIMIT and
               v.identity.key == 'b' for v in result.violations)


def test_business_identity_drift_is_detected():
    identity = make_identity('stable-ref')
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, status=consts.SMPP_ESME_RTHROTTLED,
                            reference='different-ref'),
    ]
    result = retry.verify(events)
    assert retry.VIOLATION_IDENTITY_DRIFT in {v.code for v in result.violations}


def test_duplicate_enqueue_and_unknown_job_are_detected():
    identity = make_identity()
    stray = retry.BusinessIdentity('stray')
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.enqueue(1, identity),
        retry.Event.attempt(2, stray, status=consts.SMPP_ESME_ROK),
    ]
    result = retry.verify(events)
    codes = {v.code for v in result.violations}
    assert retry.VIOLATION_DUPLICATE_ENQUEUE in codes
    assert retry.VIOLATION_UNKNOWN_JOB in codes


def test_attempt_after_terminal_state_is_detected():
    identity = make_identity()
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, status=consts.SMPP_ESME_RINVDSTADR),
        retry.Event.attempt(2, identity, status=consts.SMPP_ESME_ROK),
    ]
    result = retry.verify(events)
    assert retry.VIOLATION_AFTER_TERMINAL in {v.code for v in result.violations}


def test_attempt_before_scheduled_time_is_detected():
    identity = make_identity()
    planner = retry.RetryPlanner(
        backoff=retry.BackoffPolicy(initial_delay=10, max_attempts=3))
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, status=consts.SMPP_ESME_RTHROTTLED),
        retry.Event.attempt(2, identity, status=consts.SMPP_ESME_ROK),
    ]
    result = retry.simulate(events, planner)
    assert retry.VIOLATION_BEFORE_SCHEDULE in {v.code for v in result.violations}


def test_full_timeline_with_window_backoff_and_reconciliation():
    identity = make_identity('order-42')
    planner = retry.RetryPlanner(
        backoff=retry.BackoffPolicy(initial_delay=2, multiplier=2, max_attempts=4),
        send_window=retry.SendWindow((retry.Window(0, 100),)),
        rate_limiter=retry.RateLimiter(limit=5, interval=10))
    events = [
        retry.Event.enqueue(0, identity),
        retry.Event.attempt(1, identity, interrupted=True, sequence=10),
        retry.Event.reconcile(3, identity, accepted=False),
        retry.Event.attempt(5, identity, status=consts.SMPP_ESME_RTHROTTLED,
                            sequence=11, reference='order-42'),
        retry.Event.attempt(9, identity, status=consts.SMPP_ESME_ROK,
                            sequence=12, reference='order-42'),
    ]
    result = retry.simulate(events, planner)
    assert result.ok
    assert result.state_of(identity) == retry.STATE_SUCCEEDED
    actions = [entry.action for entry in result.timeline]
    assert actions == [
        retry.ACTION_DISPATCH,
        retry.ACTION_HOLD_RECONCILIATION,
        retry.ACTION_RETRY,
        retry.ACTION_RETRY,
        retry.ACTION_SUCCEEDED,
    ]
