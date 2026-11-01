"""Startup schema step for the credits tables.

create_all only builds indexes when it builds the table, so a database that already
has token_transaction needs this index added by hand. It is the one thing that makes
Stripe crediting exactly-once: without it the webhook falls back to its SELECT check
and two overlapping deliveries can both credit (hivemind ticket #795).

Runs on every boot from init_db(). Adding an index changes no rows. If a database
already holds duplicates the index cannot be built, and that must not stop the site
from starting, so it is skipped with an error in the log.
"""

import logging

from sqlalchemy import Connection, func, inspect, select

from core.credits.models import STRIPE_IDEMPOTENCY_INDEX, TokenTransaction

logger = logging.getLogger(__name__)


def ensure_stripe_idempotency_index(conn: Connection) -> bool:
    """Add the unique (stripe_id, type) index if missing. True when it is in place."""
    table = TokenTransaction.__table__
    if any(i["name"] == STRIPE_IDEMPOTENCY_INDEX for i in inspect(conn).get_indexes(table.name)):
        return True

    duplicated = (
        select(table.c.stripe_id)
        .where(table.c.stripe_id.is_not(None))
        .group_by(table.c.stripe_id, table.c.type)
        .having(func.count() > 1)
    )
    count = conn.scalar(select(func.count()).select_from(duplicated.subquery()))
    if count:
        logger.error(
            "%s NOT created: %d duplicate (stripe_id, type) pair(s) in %s. Stripe crediting "
            "is not exactly-once on this database until they are resolved by hand.",
            STRIPE_IDEMPOTENCY_INDEX, count, table.name,
        )
        return False

    index = next(i for i in table.indexes if i.name == STRIPE_IDEMPOTENCY_INDEX)
    index.create(conn)
    logger.info("Created %s on %s", STRIPE_IDEMPOTENCY_INDEX, table.name)
    return True
