"""Shared test safety net (T034c).

Top-up initiation can call Stripe whenever STRIPE_SECRET_KEY is configured, and some
test modules load the developer's .env at import time. Without this fixture a unit
test that reaches ``initiate_top_up`` could make a REAL network call to Stripe with a
real (sandbox) key. Every test therefore starts with both Stripe variables removed;
a test that needs them sets them explicitly (``monkeypatch.setenv`` / ``patch.dict``),
which keeps the dependency visible in the test itself.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_ambient_stripe_configuration(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
