import os

# Default suitable for Linux hosts that provide a persistent disk path.
os.environ.setdefault("HIDENCLOUD", "1")

from app import app, init_db

init_db()

# Common WSGI variable name
application = app
