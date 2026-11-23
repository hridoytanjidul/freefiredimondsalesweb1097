"""Command-line helpers.

    python3 manage.py create-admin you@example.com "Your Name"     # asks for a password
    python3 manage.py set-password you@example.com                 # reset any user's password
    python3 manage.py make-admin you@example.com                   # give an existing user admin access
"""
import getpass
import os
import sys


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from db import connect, init_db  # noqa: E402
from helpers import hash_password  # noqa: E402

DB_PATH = (os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PATH")
           or os.path.join(BASE_DIR, "instance", "topup.db"))


def ask_password():
    pw = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Password (min 8 chars): ")
    if len(pw) < 8:
        sys.exit("Password must be at least 8 characters.")
    if not os.environ.get("ADMIN_PASSWORD") and pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    return pw


def main(argv):
    if len(argv) < 3 or argv[1] not in ("create-admin", "set-password", "make-admin"):
        sys.exit(__doc__)
    init_db(DB_PATH)
    cmd, email = argv[1], argv[2].strip().lower()
    conn = connect(DB_PATH)
    user = conn.execute("SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)).fetchone()
    if cmd == "create-admin":
        if user:
            sys.exit("A user with that email already exists. Use make-admin or set-password.")
        name = argv[3] if len(argv) > 3 else "Admin"
        conn.execute("INSERT INTO users (name, email, password_hash, is_admin) VALUES (?,?,?,1)",
                     (name, email, hash_password(ask_password())))
        print("Admin created: %s  ->  log in at /login, then open /admin" % email)
    elif not user:
        sys.exit("No user with email %s" % email)
    elif cmd == "set-password":
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(ask_password()), user["id"]))
        print("Password updated for %s" % email)
    elif cmd == "make-admin":
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user["id"],))
        print("%s is now an admin" % email)
    conn.close()


if __name__ == "__main__":
    main(sys.argv)
