-- Install required PostgreSQL extensions.
-- Init scripts run in alphabetical order, so we use the 00- prefix to
-- ensure extensions are in place before any user data is loaded.

CREATE EXTENSION IF NOT EXISTS vector;
