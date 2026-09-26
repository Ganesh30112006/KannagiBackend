"""Create missing tables and default data (safe to run again; existing data is kept).

    python -m app.setup_db                                   # uses DATABASE_URL from .env
    DATABASE_URL="postgresql://...neon.tech/kannagi_mart?sslmode=require" python -m app.setup_db
"""

from sqlalchemy import inspect

from .database import engine
from .startup import prepare_database


def main() -> None:
    target = engine.url.render_as_string(hide_password=True)
    before = set(inspect(engine).get_table_names())
    added = prepare_database()  # the same as every API start
    created = sorted(set(inspect(engine).get_table_names()) - before)
    print(f"Database: {target}")
    print(f"Created tables: {', '.join(created) if created else 'none (all present)'}")
    if added:
        print(f"Added columns: {', '.join(added)}")
    print("Default offers are in place (no products: add them in the dashboard).")


if __name__ == "__main__":
    main()
