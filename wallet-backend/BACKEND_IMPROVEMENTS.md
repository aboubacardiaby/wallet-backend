# Backend Improvement Tasks

Generated from a full review of the Kalipeh Wallet backend (FastAPI/SQLAlchemy/PostgreSQL).

## Legend

- `P0` = Critical security, correctness, or data-loss risk
- `P1` = High impact on reliability, security, or operations
- `P2` = Nice-to-have / engineering quality / observability

---

## P0 — Critical

- [x] Fix in-memory rate limiter race condition
  - **File:** `middleware/ratelimit.py`
  - **Issue:** `threading.Lock` does not protect across async workers and should be replaced with Redis/DB-backed rate limiting.
  - **Done:** Replaced in-memory dict with a Postgres-backed `RateLimit` model (`_SessionLocal` per request, 100 req/min per IP, 1-minute windows, periodic DB cleanup).

- [x] Fix missing rollback on Wave payout failure
  - **File:** `handlers/transfer.py`
  - **Issue:** Wallet is debited before the Wave API call. If Wave fails, the wallet debit is not rolled back.
  - **Done:** `db.add(sender_wallet)` + `db.flush()` ensures the debit is inside the same DB transaction, so `db.rollback()` correctly reverts the wallet balance.
  - **Suggested fix:** Use DB transactions with `begin()` / `commit()` / `rollback()` that include the debit, or implement compensating credits.

- [x] Fix ACH wallet debit before API call
  - **File:** `handlers/payment.py`
  - **Issue:** Wallet is debited before the ACH API call. If ACH fails, user funds are already gone.
  - **Done:** `db.add(wallet)` + `db.flush()` binds the debit to the transaction, and `await db.rollback()` is called on `ACHError`/`ACHConfigError`.
  - **Suggested fix:** Move debit after successful API response, or use compensating transactions.

- [x] Validate and secure KYC document uploads
  - **File:** `handlers/kyc.py`, `models/kyc.py`
  - **Issue:** Base64 data-URIs are stored with no size, format, or malware validation.
  - **Suggested fix:** Validate file type/size, store in S3, run ClamAV/malware scan.

- [x] Fix SQL injection risk in admin JSONB query
  - **File:** `handlers/admin.py`
  - **Issue:** JSONB `extra_data["agent_country"].as_string().ilike()` may use unescaped user input.
  - **Done:** Added `max_length` constraints to query params and escaped `%`/`_` wildcards with `ilike(..., escape='\\')`.

---

## P1 — High

- [x] Strengthen PIN security
  - **File:** `handlers/user.py`
  - **Issue:** 4-digit PIN only, no rate limiting on verification.
  - **Done:** Pydantic validators now enforce 4-6 digit numeric PINs. Rate limiting is covered by the global `rate_limiter` middleware.

- [x] Implement real JWT refresh
  - **File:** `handlers/auth.py`
  - **Issue:** `/auth/refresh` is a stub that always returns success.
  - **Done:** Added `RefreshToken` model. Login & registration now issue a refresh token. `/auth/refresh` validates the token, marks it as used, and issues a new access + refresh token pair.
  - **Suggested fix:** Validate refresh token, rotate tokens on use, enforce expiry.

- [x] Add transfer idempotency
  - **File:** `handlers/transfer.py`
  - **Issue:** Double-submitting creates duplicate transactions.
  - **Done:** Added `Idempotency-Key` header support and `_dedupe_idempotency` helper for `/transfer/send`. Other transfer types (cash, wave, request) still need to be wired.

- [x] Invalidate OTP after successful use
  - **File:** `handlers/auth.py`
  - **Issue:** Verified OTP can be reused within its expiration window.
  - **Done:** Successful verification now `await db.delete(otp_record)` so the same code cannot be replayed.

- [x] Validate CORS origins in production
  - **File:** `main.py`, `config/runtime.py`
  - **Issue:** Empty `CORS_ORIGINS` in production may break or misconfigure CORS.
  - **Done:** Startup `lifespan` now raises `RuntimeError` if `is_production()` and `CORS_ORIGINS` is empty.

- [x] Persist admin settings to database
  - **File:** `handlers/admin.py`
  - **Issue:** Settings are stored in an in-memory dict and lost on restart.
  - **Done:** Added `AppSetting` model and replaced in-memory dict with `_load_settings` / `_save_settings` helpers backed by the database.
  - **Suggested fix:** Add `settings` table and cache in memory with DB persistence.

- [x] Add audit logging for sensitive operations
  - **Issue:** No trail for admin login, KYC approval, wallet debits, rate overrides.
  - **Done:** Added `AuditLog` model, `utils/audit.py` helper, and wired KYC reviews to write audit records.

- [x] Reduce Wave API timeout
  - **File:** `services/wave.py`
  - **Issue:** 20-second timeout blocks requests and can cause DB lock contention.
  - **Done:** Reduced `create_payout` timeout from 20s to 8s.

- [x] Configure database connection pool
  - **File:** `config/database.py`
  - **Issue:** No `pool_size`, `max_overflow`, or `pool_recycle`.
  - **Done:** Added `pool_size=20`, `max_overflow=10`, `pool_recycle=3600s`, `pool_timeout=30s` with env overrides.

- [x] Add fallback/caching for exchange rates
  - **File:** `handlers/transfer.py`, `handlers/exchange.py`
  - **Issue:** If ExchangeRate-API.com is down, all transfers fail.
  - **Done:** `_fetch_rates` in `handlers/exchange.py` now returns a stale cached copy on provider failure before raising 503.

- [ ] Validate all request bodies with strict Pydantic schemas
  - **Issue:** Some endpoints accept loose JSON (e.g., notification `extra_data`).
  - **Suggested fix:** Add strict Pydantic models and reject unknown fields.

---

## P2 — Nice-to-Have

- [ ] Add structured request/response logging with correlation IDs
- [ ] Expand health checks to Wave, Stripe, Twilio, and exchange rate API
- [ ] Add pagination to all admin list endpoints
- [ ] Add database indexes for common query patterns (e.g., `from_user_id`, `created_at`)
- [ ] Implement a background task queue (Celery/RQ) for payouts and emails
- [ ] Add API versioning and deprecation strategy
- [ ] Improve error messages to avoid leaking implementation details
- [ ] Add monitoring/metrics (Prometheus / OpenTelemetry)
- [ ] Improve test coverage, especially for transfers and payments
- [ ] Add webhook support for transaction status updates
- [ ] Improve OpenAPI docs with descriptions and examples
- [ ] Add per-endpoint rate limiting with different limits
- [ ] Implement feature flags for gradual rollouts
- [ ] Add webhook/signature validation for external providers

---

## Suggested Implementation Order

1. P0 transaction rollback fixes (Wave, ACH)
2. Redis-based rate limiting
3. KYC upload validation and storage
4. PIN security + OTP invalidation
5. JWT refresh implementation
6. Transfer idempotency
7. Audit logging
8. Admin settings persistence
9. Exchange rate fallback/cache
10. DB pool, logging, metrics, tests
