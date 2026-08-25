import pytest


@pytest.fixture(autouse=True)
def _reset_subscription_rate_limiter():
    """PH2-06: the subscription-fetch rate limiter is a single shared
    module-level instance (matching production, which is single-process).
    Many unrelated tests call `handle_sub`/`handle_opaque_sub` with a fake
    handler that resolves to the same fallback IP -- reset before every
    test so none of them can accumulate budget across test boundaries."""
    from src.subscription_rate_limit import SUBSCRIPTION_FETCH_LIMITER
    SUBSCRIPTION_FETCH_LIMITER.clear()
    yield
    SUBSCRIPTION_FETCH_LIMITER.clear()
