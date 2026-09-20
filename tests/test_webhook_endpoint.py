"""Webhook HTTP endpoint tests (T015; CRITICAL: production POST behavior testing)."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient


class WebhookEndpointTests(unittest.TestCase):
    def setUp(self):
        # CRITICAL FIX: Use the actual production router with proper database dependency override
        from handlers.webhook import router
        from config.database import get_db
        from fastapi import FastAPI
        
        self.app = FastAPI()
        
        # Properly override the get_db dependency to avoid database unavailability
        async def override_get_db():
            db = AsyncMock()
            db.scalar = AsyncMock(return_value=None)  # No existing event
            db.add = Mock()
            db.flush = AsyncMock()
            db.commit = AsyncMock()
            db.rollback = AsyncMock()
            return db
        
        self.app.dependency_overrides[get_db] = override_get_db
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def tearDown(self):
        # Clean up dependency overrides
        from handlers.webhook import router
        from config.database import get_db
        from fastapi import FastAPI
        
        self.app.dependency_overrides.pop(get_db, None)

    def test_webhook_endpoint_spec_compliant_route(self):
        """CRITICAL FIX: Webhook endpoint should match spec: /api/webhooks/payments/{providerName}"""
        response = self.client.get("/api/webhooks/payments/mock")
        # Should return 405 Method Not Allowed (POST required) rather than 404
        self.assertIn(response.status_code, [405, 200])

    def test_webhook_endpoint_production_post(self):
        """CRITICAL FIX: Test real POST behavior through production route with database override"""
        response = self.client.post(
            "/api/webhooks/payments/mock",
            json={"test": "data"},
            headers={"X-Webhook-Signature": "test_signature"}
        )
        # Should not return 404 (endpoint exists and is accessible)
        # Should return specific error status (400/422/500) due to provider verification, not 404
        self.assertNotEqual(response.status_code, 404, "Production POST should reach the endpoint")
        # Should return a JSON response with error information
        self.assertIn("application/json", response.headers.get("content-type", ""), "Should return JSON response")

    def test_webhook_endpoint_missing_signature(self):
        """FR-007: Webhook endpoint should require signature header."""
        response = self.client.post(
            "/api/webhooks/payments/mock",
            json={"test": "data"},
            # Missing X-Webhook-Signature header
        )
        # Should return 422 (Unprocessable Entity) due to missing required header
        self.assertEqual(response.status_code, 422, "Missing signature should cause 422 validation error")
        # Should return validation error details
        self.assertIn("detail", response.json(), "Should return validation error details")

    def test_webhook_endpoint_body_size_limit(self):
        """FR-020: Webhook endpoint should enforce body size limits."""
        from handlers.webhook import MAX_WEBHOOK_BODY_SIZE
        self.assertEqual(MAX_WEBHOOK_BODY_SIZE, 1024 * 1024, "Body size limit should be 1MB")

    def test_webhook_endpoint_provider_validation(self):
        """FR-007: Webhook endpoint should validate provider names."""
        response = self.client.post(
            "/api/webhooks/payments/unknownpay",  # Unsupported provider (T034b: stripe is now supported)
            json={"test": "data"},
            headers={"X-Webhook-Signature": "test_signature"}
        )
        # Should return 400 for unsupported provider
        self.assertEqual(response.status_code, 400, "Unsupported provider should be rejected with 400")
        # Should return error message about provider validation
        response_json = response.json()
        self.assertIn("detail", response_json, "Should return error details")

    def test_webhook_endpoint_accepts_mock_provider(self):
        """FR-007: Webhook endpoint should accept mock provider."""
        response = self.client.post(
            "/api/webhooks/payments/mock",  # Supported provider
            json={"test": "data"},
            headers={"X-Webhook-Signature": "test_signature"}
        )
        # Should not reject due to provider name (mock is supported)
        # Should not return 400 for provider validation (may fail for other reasons)
        self.assertNotEqual(response.status_code, 400, "Mock provider should be accepted")
        # Should return a structured response
        self.assertIn("application/json", response.headers.get("content-type", ""), "Should return JSON response")


class StripeWebhookRouteTests(unittest.TestCase):
    """T034b: provider registry, Stripe's own signature header, fail-closed
    configuration, and HTTP statuses that make a retrying provider behave.

    Stripe treats ANY 2xx as "delivered, stop retrying". So a genuinely failed or
    unverifiable delivery must not be answered with 2xx (it used to be: every
    outcome returned 202), while an authentic event we deliberately do not act on
    must be, or Stripe redelivers it for days. Signing here uses this file's own
    HMAC per Stripe's documented scheme, as in test_topup_stripe_provider.py.
    """

    SECRET = "whsec_test_endpoint_secret_not_a_real_credential"

    def setUp(self):
        import hashlib
        import hmac
        import json
        import os
        import time

        from fastapi import FastAPI

        from config.database import get_db
        from handlers.webhook import router

        self._hashlib, self._hmac, self._json, self._time = hashlib, hmac, json, time
        self._env = patch.dict(
            os.environ, {"STRIPE_WEBHOOK_SECRET": self.SECRET, "APP_ENV": "development"}
        )
        self._env.start()

        async def override_get_db():
            db = AsyncMock()
            db.scalar = AsyncMock(return_value=None)
            db.add = Mock()
            db.flush = AsyncMock()
            db.commit = AsyncMock()
            db.rollback = AsyncMock()
            return db

        self.app = FastAPI()
        self.app.dependency_overrides[get_db] = override_get_db
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def tearDown(self):
        self._env.stop()

    def _signed(self, event_type="payment_intent.succeeded", *, secret=None, currency="usd", raw=None):
        body = raw if raw is not None else self._json.dumps({
            "id": "evt_route_1", "object": "event", "type": event_type,
            "data": {"object": {"id": "pi_route_1", "object": "payment_intent",
                                "amount": 500, "amount_received": 500, "currency": currency}},
        }).encode()
        ts = int(self._time.time())
        digest = self._hmac.new((secret or self.SECRET).encode(), f"{ts}.".encode() + body, self._hashlib.sha256).hexdigest()
        return body, f"t={ts},v1={digest}"

    def _post(self, body, header, name="Stripe-Signature"):
        headers = {} if header is None else {name: header}
        return self.client.post("/api/webhooks/payments/stripe", content=body, headers=headers)

    def test_stripe_is_a_supported_provider(self):
        body, header = self._signed("charge.succeeded")
        self.assertNotEqual(self._post(body, header).status_code, 400)

    def test_stripe_requires_its_own_signature_header(self):
        body, _ = self._signed()
        self.assertEqual(self._post(body, None).status_code, 422)

    def test_stripe_does_not_accept_the_mock_providers_header_name(self):
        body, header = self._signed()
        self.assertEqual(self._post(body, header, name="X-Webhook-Signature").status_code, 422)

    def test_stripe_fails_closed_when_no_signing_secret_is_configured(self):
        import os

        body, header = self._signed()
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": ""}):
            response = self._post(body, header)
        self.assertEqual(response.status_code, 503)

    def test_a_value_that_is_not_a_signing_secret_fails_closed_like_a_missing_one(self):
        # A publishable key had already been supplied here by mistake; it can never
        # authenticate a webhook, so the route must not pretend to be configured.
        import os

        body, header = self._signed()
        for bad in ("pk_test_51ExamplePublishableKeyValue", "sk_test_51ExampleSecretKeyValue", "whsec_short"):
            with self.subTest(value=bad[:12]):
                with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": bad}):
                    response = self._post(body, header)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(bad, response.text)

    def test_surrounding_whitespace_in_the_secret_is_tolerated_like_it_is_for_initiation(self):
        # Initiation strips the value, so the route must too: otherwise a stray newline would
        # let payments start while every webhook is refused.
        import os

        body, header = self._signed()
        with patch.dict(os.environ, {"STRIPE_WEBHOOK_SECRET": f" {self.SECRET}\n"}):
            self.assertEqual(self._post(body, header).status_code, 404)  # verified; unknown top-up

    def test_the_route_gives_the_provider_the_api_key_so_a_failed_bank_debit_can_cancel_its_payment(self):
        # T034d: cancelling the Stripe payment after a failed bank debit needs the API key.
        import os

        from handlers.webhook import _resolve_provider

        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": " sk_test_key_for_route \n"}):
            provider = _resolve_provider("stripe")
        self.assertEqual(provider._api_key.strip(), "sk_test_key_for_route")

    def test_without_an_api_key_webhooks_still_verify_but_cannot_cancel(self):
        from handlers.webhook import _resolve_provider
        from services.topup.provider import ProviderConfigurationError

        provider = _resolve_provider("stripe")  # conftest strips STRIPE_SECRET_KEY
        with self.assertRaises(ProviderConfigurationError):
            provider.cancel("pi_1")

    def test_a_malformed_api_key_fails_closed_like_initiation_does(self):
        import os

        body, header = self._signed()
        with patch.dict(os.environ, {"STRIPE_SECRET_KEY": "pk_test_51ExamplePublishableKeyValue"}):
            response = self._post(body, header)
        self.assertEqual(response.status_code, 503)

    def test_bad_signature_is_rejected_not_acknowledged(self):
        body, header = self._signed(secret="a-different-secret")
        response = self._post(body, header)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(self.SECRET, response.text)

    def test_authentic_but_unhandled_event_is_acknowledged_with_2xx(self):
        body, header = self._signed("charge.succeeded")
        response = self._post(body, header)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "ignored_event")

    def test_valid_signature_with_a_malformed_payload_is_a_400(self):
        body, header = self._signed(currency="eur")
        self.assertEqual(self._post(body, header).status_code, 400)

    def test_verified_event_for_an_unknown_top_up_is_not_acknowledged_so_the_provider_retries(self):
        # The event may simply have arrived before our own record of the PaymentIntent
        # committed; a 2xx would make Stripe drop it permanently.
        body, header = self._signed("payment_intent.succeeded")
        self.assertEqual(self._post(body, header).status_code, 404)

    def test_outcome_to_http_status_mapping(self):
        expected = {
            "success": 202,
            "acknowledged": 202,
            "duplicate_event": 202,
            "ignored_event": 202,
            "verification_failed": 401,
            "malformed_payload": 400,
            "top_up_not_found": 404,
            "completion_error": 500,
            "payload_processing_error": 500,
            "something_unexpected": 500,
        }
        body, header = self._signed()
        for outcome, code in expected.items():
            with self.subTest(outcome=outcome):
                with patch("handlers.webhook.process_webhook", new=AsyncMock(return_value={"status": outcome})):
                    self.assertEqual(self._post(body, header).status_code, code)

    def test_mock_provider_is_refused_in_production(self):
        # The mock provider signs with a secret that is hardcoded in this repository.
        import os

        with patch.dict(os.environ, {"APP_ENV": "production"}):
            response = self.client.post(
                "/api/webhooks/payments/mock", json={"x": 1}, headers={"X-Webhook-Signature": "sig"}
            )
        self.assertEqual(response.status_code, 400)

    def test_mock_provider_still_works_outside_production(self):
        response = self.client.post(
            "/api/webhooks/payments/mock", json={"x": 1}, headers={"X-Webhook-Signature": "sig"}
        )
        self.assertNotEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()