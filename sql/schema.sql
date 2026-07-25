-- =============================================================================
-- Kalipeh Wallet — full database schema
-- =============================================================================
-- Generated from the SQLAlchemy models (models/*.py), which are this
-- project's source of truth for schema. Every table, column, default,
-- foreign key, and index defined in the ORM is included below.
--
-- Usage — run against ANY empty PostgreSQL database, standalone:
--   psql "$DATABASE_URL" -f sql/schema.sql
--
-- Idempotent: every statement uses IF NOT EXISTS, so re-running this script
-- against a database that already has some/all of these objects is safe and
-- will not drop or alter existing tables (matches the app's own
-- config/database.py auto-create behavior).
--
-- Requirements: PostgreSQL 12+. No extensions required — primary keys are
-- UUIDs generated application-side (Python `uuid.uuid4()`), not by the
-- database, so no pgcrypto/uuid-ossp extension is needed.
--
-- Note: created_at/updated_at/id defaults are applied by the application
-- (SQLAlchemy `default=...`), not by the database (no column DEFAULT for
-- them below). Rows inserted directly via SQL — outside the app — must
-- supply these columns explicitly.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Platform / admin configuration
-- -----------------------------------------------------------------------------

-- Singleton ACH payment-provider configuration (always id = 1)
CREATE TABLE IF NOT EXISTS ach_config (
	id INTEGER NOT NULL,
	api_base_url VARCHAR(500) DEFAULT 'http://localhost:3000/v1' NOT NULL,
	api_key VARCHAR(500) DEFAULT '' NOT NULL,
	platform_account_number VARCHAR(50) DEFAULT '' NOT NULL,
	platform_routing_number VARCHAR(9) DEFAULT '' NOT NULL,
	platform_account_type VARCHAR(20) DEFAULT 'CHECKING' NOT NULL,
	platform_account_name VARCHAR(100) DEFAULT 'Kalipeh Platform' NOT NULL,
	enabled BOOLEAN DEFAULT 'false' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_ach_config PRIMARY KEY (id)
);

-- Admin/back-office operator accounts
CREATE TABLE IF NOT EXISTS admin_users (
	id UUID NOT NULL,
	username VARCHAR(50) NOT NULL,
	email VARCHAR(200),
	password_hash VARCHAR(255) NOT NULL,
	role VARCHAR(30) DEFAULT 'viewer' NOT NULL,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	created_by UUID,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_admin_users PRIMARY KEY (id),
	CONSTRAINT fk_admin_users_created_by_admin_users FOREIGN KEY(created_by) REFERENCES admin_users (id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_admin_users_username ON admin_users (username);

-- Bank directory used by transfer/bank-linking flows
CREATE TABLE IF NOT EXISTS banks (
	id UUID NOT NULL,
	name VARCHAR(100) NOT NULL,
	country VARCHAR(100) NOT NULL,
	country_code VARCHAR(5),
	swift_code VARCHAR(20),
	logo_url TEXT,
	currency VARCHAR(10),
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_banks PRIMARY KEY (id)
);

-- Admin-configured transfer fee rules (matched by currency pair / amount range)
CREATE TABLE IF NOT EXISTS fee_rules (
	id UUID NOT NULL,
	name VARCHAR(100) NOT NULL,
	from_currency VARCHAR(10),
	to_currency VARCHAR(10),
	fee_rate NUMERIC(8, 6) DEFAULT '0.015' NOT NULL,
	fee_flat NUMERIC(18, 2) DEFAULT '0' NOT NULL,
	min_fee NUMERIC(18, 2),
	max_fee NUMERIC(18, 2),
	min_amount NUMERIC(18, 2),
	max_amount NUMERIC(18, 2),
	priority INTEGER NOT NULL,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	note TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_fee_rules PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_fee_rules_from_currency ON fee_rules (from_currency);
CREATE INDEX IF NOT EXISTS ix_fee_rules_to_currency ON fee_rules (to_currency);

-- Admin-set manual FX rate overrides (replace live market rate when active)
CREATE TABLE IF NOT EXISTS rate_overrides (
	id UUID NOT NULL,
	from_currency VARCHAR(10) NOT NULL,
	to_currency VARCHAR(10) NOT NULL,
	rate NUMERIC(18, 8) NOT NULL,
	spread_pct FLOAT DEFAULT '0' NOT NULL,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	note TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_rate_overrides PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_rate_overrides_from_currency ON rate_overrides (from_currency);
CREATE INDEX IF NOT EXISTS ix_rate_overrides_to_currency ON rate_overrides (to_currency);

-- Singleton outbound-email (SMTP) configuration (always id = 1)
CREATE TABLE IF NOT EXISTS smtp_settings (
	id INTEGER NOT NULL,
	host VARCHAR(200) DEFAULT '' NOT NULL,
	port INTEGER DEFAULT '587' NOT NULL,
	username VARCHAR(200) DEFAULT '' NOT NULL,
	password VARCHAR(500) DEFAULT '' NOT NULL,
	from_email VARCHAR(200) DEFAULT '' NOT NULL,
	from_name VARCHAR(100) DEFAULT 'Kalipeh' NOT NULL,
	use_tls BOOLEAN DEFAULT 'true' NOT NULL,
	use_ssl BOOLEAN DEFAULT 'false' NOT NULL,
	enabled BOOLEAN DEFAULT 'false' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_smtp_settings PRIMARY KEY (id)
);

-- -----------------------------------------------------------------------------
-- OTPs (issued before a user row necessarily exists — no FK to users)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS otps (
	id UUID NOT NULL,
	phone_number VARCHAR(20) NOT NULL,
	code VARCHAR(64) NOT NULL,
	purpose VARCHAR(50) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	verified BOOLEAN DEFAULT 'false' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_otps PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_otps_phone_number ON otps (phone_number);

-- -----------------------------------------------------------------------------
-- Users
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
	id UUID NOT NULL,
	phone_number VARCHAR(20) NOT NULL,
	country_code VARCHAR(10) DEFAULT '' NOT NULL,
	full_name VARCHAR(255) DEFAULT '' NOT NULL,
	email VARCHAR(255) DEFAULT '' NOT NULL,
	pin VARCHAR(255) DEFAULT '' NOT NULL,
	pin_attempts INTEGER DEFAULT '0' NOT NULL,
	is_locked BOOLEAN DEFAULT 'false' NOT NULL,
	is_verified BOOLEAN DEFAULT 'false' NOT NULL,
	kyc_status VARCHAR(50) DEFAULT 'pending' NOT NULL,
	profile_photo VARCHAR(500) DEFAULT '' NOT NULL,
	national_id_type VARCHAR(50) DEFAULT '' NOT NULL,
	national_id_number VARCHAR(100) DEFAULT '' NOT NULL,
	date_of_birth TIMESTAMP WITH TIME ZONE,
	street VARCHAR(255) DEFAULT '' NOT NULL,
	city VARCHAR(100) DEFAULT '' NOT NULL,
	region VARCHAR(100) DEFAULT '' NOT NULL,
	country VARCHAR(100) DEFAULT '' NOT NULL,
	postal_code VARCHAR(20) DEFAULT '' NOT NULL,
	user_type VARCHAR(20) DEFAULT 'receiver' NOT NULL,
	home_currency VARCHAR(10) DEFAULT 'XOF' NOT NULL,
	preferred_lang VARCHAR(10) DEFAULT 'fr' NOT NULL,
	biometric_enabled BOOLEAN DEFAULT 'false' NOT NULL,
	device_tokens VARCHAR[] DEFAULT '{}' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_login_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_users PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone_number ON users (phone_number);

-- -----------------------------------------------------------------------------
-- Agents (cash-in/cash-out points)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS agents (
	id UUID NOT NULL,
	user_id UUID,
	business_name VARCHAR(255) NOT NULL,
	phone_number VARCHAR(20) NOT NULL,
	address VARCHAR(500) DEFAULT '' NOT NULL,
	country VARCHAR(100) DEFAULT '' NOT NULL,
	latitude FLOAT DEFAULT '0' NOT NULL,
	longitude FLOAT DEFAULT '0' NOT NULL,
	status VARCHAR(20) DEFAULT 'active' NOT NULL,
	is_active BOOLEAN DEFAULT 'true' NOT NULL,
	rating FLOAT DEFAULT '0' NOT NULL,
	total_ratings INTEGER DEFAULT '0' NOT NULL,
	cash_in_limit NUMERIC(18, 2) DEFAULT '0' NOT NULL,
	cash_out_limit NUMERIC(18, 2) DEFAULT '0' NOT NULL,
	commission FLOAT DEFAULT '0' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_agents PRIMARY KEY (id),
	CONSTRAINT fk_agents_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_agents_user_id ON agents (user_id);

-- -----------------------------------------------------------------------------
-- KYC
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS kyc_submissions (
	id UUID NOT NULL,
	user_id UUID NOT NULL,
	full_name VARCHAR(255) DEFAULT '' NOT NULL,
	date_of_birth VARCHAR(20) DEFAULT '' NOT NULL,
	nationality VARCHAR(100) DEFAULT '' NOT NULL,
	address VARCHAR(500) DEFAULT '' NOT NULL,
	city VARCHAR(100) DEFAULT '' NOT NULL,
	country VARCHAR(100) DEFAULT '' NOT NULL,
	id_type VARCHAR(50) DEFAULT '' NOT NULL,
	id_number VARCHAR(100) DEFAULT '' NOT NULL,
	id_expiry VARCHAR(20) DEFAULT '' NOT NULL,
	id_front_url TEXT DEFAULT '' NOT NULL,
	id_back_url TEXT DEFAULT '' NOT NULL,
	selfie_url TEXT DEFAULT '' NOT NULL,
	status VARCHAR(20) DEFAULT 'pending' NOT NULL,
	rejection_reason TEXT,
	reviewed_by VARCHAR(255),
	reviewed_at TIMESTAMP WITH TIME ZONE,
	extra JSONB,
	submitted_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_kyc_submissions PRIMARY KEY (id),
	CONSTRAINT fk_kyc_submissions_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_kyc_submissions_user_id ON kyc_submissions (user_id);

-- -----------------------------------------------------------------------------
-- Money requests (peer-to-peer request-for-payment)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS money_requests (
	id UUID NOT NULL,
	from_user_id UUID NOT NULL,
	to_user_id UUID NOT NULL,
	amount NUMERIC(18, 2) NOT NULL,
	description TEXT DEFAULT '' NOT NULL,
	status VARCHAR(20) DEFAULT 'pending' NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_money_requests PRIMARY KEY (id),
	CONSTRAINT fk_money_requests_from_user_id_users FOREIGN KEY(from_user_id) REFERENCES users (id) ON DELETE CASCADE,
	CONSTRAINT fk_money_requests_to_user_id_users FOREIGN KEY(to_user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_money_requests_from_user_id ON money_requests (from_user_id);
CREATE INDEX IF NOT EXISTS ix_money_requests_to_user_id ON money_requests (to_user_id);

-- -----------------------------------------------------------------------------
-- Notifications
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS notifications (
	id UUID NOT NULL,
	user_id UUID NOT NULL,
	type VARCHAR(50) NOT NULL,
	title VARCHAR(255) NOT NULL,
	message TEXT NOT NULL,
	data JSONB,
	is_read BOOLEAN DEFAULT 'false' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	read_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_notifications PRIMARY KEY (id),
	CONSTRAINT fk_notifications_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_notifications_created_at ON notifications (created_at);
CREATE INDEX IF NOT EXISTS ix_notifications_user_id ON notifications (user_id);

-- -----------------------------------------------------------------------------
-- Payment methods (tokenized cards, linked banks, PayPal, wallets)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS payment_methods (
	id UUID NOT NULL,
	user_id UUID NOT NULL,
	type VARCHAR(30) NOT NULL,
	label VARCHAR(255) DEFAULT '' NOT NULL,
	card_brand VARCHAR(20) DEFAULT '' NOT NULL,
	last4 VARCHAR(4) DEFAULT '' NOT NULL,
	stripe_payment_method_id VARCHAR(255),
	expiry_month INTEGER,
	expiry_year INTEGER,
	holder_name VARCHAR(255) DEFAULT '' NOT NULL,
	bank_name VARCHAR(255) DEFAULT '' NOT NULL,
	account_last4 VARCHAR(4) DEFAULT '' NOT NULL,
	routing_number VARCHAR(20) DEFAULT '' NOT NULL,
	account_type VARCHAR(20) DEFAULT '' NOT NULL,
	email VARCHAR(255) DEFAULT '' NOT NULL,
	is_default BOOLEAN DEFAULT 'false' NOT NULL,
	is_verified BOOLEAN DEFAULT 'true' NOT NULL,
	metadata JSONB,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_payment_methods PRIMARY KEY (id),
	CONSTRAINT fk_payment_methods_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_payment_methods_user_id ON payment_methods (user_id);

-- -----------------------------------------------------------------------------
-- Recipients (saved transfer contacts)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS recipients (
	id UUID NOT NULL,
	user_id UUID NOT NULL,
	phone_number VARCHAR(20) NOT NULL,
	full_name VARCHAR(255) DEFAULT '' NOT NULL,
	nickname VARCHAR(100) DEFAULT '' NOT NULL,
	avatar_color VARCHAR(20) DEFAULT '#6366f1' NOT NULL,
	country_code VARCHAR(10) DEFAULT 'SN' NOT NULL,
	country_name VARCHAR(100) DEFAULT 'Senegal' NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_recipients PRIMARY KEY (id),
	CONSTRAINT fk_recipients_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_recipients_user_id ON recipients (user_id);

-- -----------------------------------------------------------------------------
-- Wallets
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS wallets (
	id UUID NOT NULL,
	user_id UUID NOT NULL,
	balance NUMERIC(18, 2) DEFAULT '0' NOT NULL,
	currency VARCHAR(10) DEFAULT 'XOF' NOT NULL,
	status VARCHAR(20) DEFAULT 'active' NOT NULL,
	daily_limit NUMERIC(18, 2) DEFAULT '500000' NOT NULL,
	monthly_limit NUMERIC(18, 2) DEFAULT '2000000' NOT NULL,
	daily_spent NUMERIC(18, 2) DEFAULT '0' NOT NULL,
	monthly_spent NUMERIC(18, 2) DEFAULT '0' NOT NULL,
	last_reset_date TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_wallets PRIMARY KEY (id),
	CONSTRAINT fk_wallets_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_wallets_user_id ON wallets (user_id);

-- -----------------------------------------------------------------------------
-- Transactions (depends on users, agents, wallets)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS transactions (
	id UUID NOT NULL,
	transaction_ref VARCHAR(100) NOT NULL,
	type VARCHAR(20) NOT NULL,
	status VARCHAR(20) NOT NULL,
	from_user_id UUID,
	to_user_id UUID,
	from_phone VARCHAR(20) DEFAULT '' NOT NULL,
	to_phone VARCHAR(20) DEFAULT '' NOT NULL,
	amount NUMERIC(18, 2) NOT NULL,
	fee NUMERIC(18, 2) DEFAULT '0' NOT NULL,
	total_amount NUMERIC(18, 2) NOT NULL,
	currency VARCHAR(10) DEFAULT 'XOF' NOT NULL,
	description TEXT DEFAULT '' NOT NULL,
	agent_id UUID,
	latitude FLOAT,
	longitude FLOAT,
	extra_data JSONB,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_transactions PRIMARY KEY (id),
	CONSTRAINT fk_transactions_from_user_id_users FOREIGN KEY(from_user_id) REFERENCES users (id) ON DELETE SET NULL,
	CONSTRAINT fk_transactions_to_user_id_users FOREIGN KEY(to_user_id) REFERENCES users (id) ON DELETE SET NULL,
	CONSTRAINT fk_transactions_agent_id_agents FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_transactions_created_at ON transactions (created_at);
CREATE INDEX IF NOT EXISTS ix_transactions_from_user_id ON transactions (from_user_id);
CREATE INDEX IF NOT EXISTS ix_transactions_to_user_id ON transactions (to_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_transactions_transaction_ref ON transactions (transaction_ref);

COMMIT;
