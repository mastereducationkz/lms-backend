"""Telegram announcements: a role-gated proxy to the Support platform.

Intentionally stateless -- no models, no tables, no migration. The registry,
the queue and the delivery worker all live in Support, because that is the only
process that ingests the Telegram bot's update stream and therefore the only
one that can know which groups the bot belongs to.

That also keeps this domain clear of the `init_db()` / Alembic divergence
documented in `src/models/__init__.py`: a model registered without a migration
appears on fresh databases and is missing on migrated ones. We add no model, so
there is nothing to diverge -- and no `import src.models` cycle-breaker is
needed here either, since this package defines no ORM classes.
"""
