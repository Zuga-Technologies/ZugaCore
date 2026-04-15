-- ZugaCore migration: drop dead credit/token columns
--
-- Context
-- -------
-- Three columns were identified as write-only (Task #5 audit, 2026-04-15):
--
--   credit_ledger.amount              — legacy "credits" value, scaled from cost_usd
--                                       via the retired DOLLARS_TO_CREDITS = 1000
--                                       constant. Never read anywhere.
--
--   credit_ledger.markup_multiplier   — per-row markup used at charge time.
--                                       Intended for promo/A-B audits that never
--                                       got built. Never read.
--
--   token_balance.free_tokens_date    — was meant to enable a daily free-token
--                                       refill that never shipped. Current product
--                                       policy: one-time welcome grant, no refill.
--                                       Never read.
--
-- The Python models in core/credits/models.py have already been updated:
--   - markup_multiplier and free_tokens_date are removed from the ORM
--   - amount is kept in the ORM as a "bridge value" (new rows insert 0) so that
--     legacy databases that still have a NOT NULL constraint accept inserts.
--
-- This migration finishes the cleanup by physically removing the columns so the
-- code-side "amount: kept only to satisfy NOT NULL" comment can also go away.
--
-- Compatibility
-- -------------
-- ALTER TABLE ... DROP COLUMN is supported on:
--   - PostgreSQL 9.0+       (Railway, any modern deployment)
--   - SQLite 3.35+          (Mac Mini fleet — verify `sqlite3 --version` first)
--
-- If running against SQLite < 3.35, use the emulation script at
-- scripts/migrations/drop_dead_credit_columns_sqlite_legacy.sql
-- (create-new-table / copy / drop / rename pattern). Not shipped today — write
-- it only if you hit an old SQLite.
--
-- Execution
-- ---------
-- 1. Stop the ZugaApp / ZugaCore backend (or put it in read-only mode) —
--    writes during migration are safe with DROP COLUMN but queries that
--    reference the dropped columns will error until the new code is live.
-- 2. Back up the database. Seriously — this is not reversible.
-- 3. Run this file against each database:
--      - Mac Mini fleet: sqlite3 /Users/zugabot/Projects/<studio>/backend/data/zugaapp.db < drop_dead_credit_columns.sql
--      - Railway:        railway connect postgres  (then paste)
-- 4. Restart the backend. Verify /api/tokens/balance and /api/tokens/history
--    return 200s on a real user.
-- 5. Once stable, remove the `amount` field from core/credits/models.py and
--    delete the `amount=0` bridge writes in core/credits/manager.py (3 sites).
--    Redeploy. The `bridge value` comment in models.py is the marker.
--
-- Rollback
-- --------
-- There is no rollback. If you need to undo this, restore the backup from step 2.
-- The dropped columns' historical data cannot be recovered without it.

-- ──────────────────────────────────────────────────────────────────────

BEGIN;

ALTER TABLE credit_ledger DROP COLUMN amount;
ALTER TABLE credit_ledger DROP COLUMN markup_multiplier;
ALTER TABLE token_balance DROP COLUMN free_tokens_date;

COMMIT;

-- After running, update core/credits/models.py to remove the CreditLedger.amount
-- field and its "bridge value" comment, then remove amount=0 from the three
-- CreditLedger(...) constructor calls in core/credits/manager.py.
