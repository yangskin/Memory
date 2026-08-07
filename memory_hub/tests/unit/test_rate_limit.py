from memory_hub.auth.rate_limit import TokenRateLimiter


def test_rate_limiter_enforces_a_per_token_category_limit() -> None:
    limiter = TokenRateLimiter()
    assert limiter.allow("tok_1", "events", 2)
    assert limiter.allow("tok_1", "events", 2)
    assert not limiter.allow("tok_1", "events", 2)
    assert limiter.allow("tok_1", "context", 2)


def test_rate_limiter_reports_retry_after_for_rejected_request() -> None:
    limiter = TokenRateLimiter()
    assert limiter.check("tok_1", "board_write", 1, window_seconds=60).allowed
    rejected = limiter.check("tok_1", "board_write", 1, window_seconds=60)
    assert rejected.allowed is False
    assert 0 < rejected.retry_after_seconds <= 60