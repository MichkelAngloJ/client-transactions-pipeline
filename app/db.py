import os

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

# Load DATABASE_URL from a .env file in the project root, if present.
# An environment variable that is already set takes precedence.
load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres@localhost:5432/itus_txn"
)


def connect() -> psycopg.Connection:
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
        connect_timeout=10,
        # Hosted Postgres services often run PgBouncer in transaction mode,
        # which does not support server-side prepared statements. Disabling
        # them has no noticeable cost at this data size and works everywhere.
        prepare_threshold=None,
    )


def get_conn():
    """FastAPI dependency that opens one connection per request.

    This is sufficient at the current data size. Under real traffic, a
    psycopg_pool ConnectionPool created at startup would be used instead.
    """
    with connect() as conn:
        yield conn
