from memory_hub.auth.rate_limit import TokenRateLimiter


def test_rate_limiter_enforces_a_per_token_category_limit() -> None:
    limiter = TokenRateLimiter()
    assert limiter.allow("tok_1", "events", 2)
    assert limiter.allow("tok_1", "events", 2)
    assert not limiter.allow("tok_1", "events", 2)
    assert limiter.allow("tok_1", "context", 2)