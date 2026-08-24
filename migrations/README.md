# Database migrations

Alembic revisions are the only supported way to change persistent schemas.
Tests upgrade and downgrade a temporary SQLite database before any migration is
used against the local application database.
