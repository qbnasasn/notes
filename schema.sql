-- GSI Notes — authentication schema.
-- Applied to the dedicated `gsinotes` database (NOT openwebui's).
-- Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS users (
    id            bigserial PRIMARY KEY,
    email         text        NOT NULL UNIQUE,
    display_name  text        NOT NULL DEFAULT '',
    password_hash text        NOT NULL,
    is_admin      boolean     NOT NULL DEFAULT false,
    is_active     boolean     NOT NULL DEFAULT true,
    -- Bumping this invalidates every existing session for the user, which is how
    -- "log out everywhere" works without keeping server-side session state.
    token_version integer     NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

-- Emails are matched case-insensitively.
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (lower(email));

-- Feeds login throttling. Rows are pruned on write, so this stays small.
CREATE TABLE IF NOT EXISTS login_attempts (
    id         bigserial   PRIMARY KEY,
    email      text,
    ip         text,
    successful boolean     NOT NULL,
    at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS login_attempts_ip_at_idx    ON login_attempts (ip, at DESC);
CREATE INDEX IF NOT EXISTS login_attempts_email_at_idx ON login_attempts (lower(email), at DESC);
