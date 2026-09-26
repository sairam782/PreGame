"""Write Pregame's .env from values you type in (nothing is echoed or printed back).

Run it in your own terminal:  C:/Projects/prep-harness/.venv/Scripts/python.exe C:/Projects/prep-harness/scripts/set_env.py

1. Paste the connection string from Atlas (Connect -> Drivers -> Python). If it contains <db_password> or <password>,
   you'll be asked for the password, which is percent-encoded for you (a "$" becomes %24).
   If it has no user in it, you'll be asked for the username and password.
2. Optionally paste your Anthropic API key (hidden input; press Enter to keep the current one).
Other lines already in .env are kept. .env is git-ignored.
"""
import getpass
import re
from pathlib import Path
from urllib.parse import quote_plus

ENV = Path(__file__).resolve().parent.parent / ".env"


def read_env() -> dict:
    out = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def build_uri() -> str:
    raw = input("Atlas connection string (mongodb+srv://...): ").strip().strip('"').strip("'")
    if not raw.startswith(("mongodb+srv://", "mongodb://")):
        raise SystemExit("That doesn't look like a MongoDB connection string (it should start with mongodb+srv://).")
    placeholder = re.search(r"<(db_password|password)>", raw)
    if placeholder:
        pw = getpass.getpass("Database password (hidden): ")
        return raw.replace(placeholder.group(0), quote_plus(pw))
    scheme, rest = raw.split("://", 1)
    if "@" in rest.split("/", 1)[0]:
        return raw                                   # user and password already in the string
    user = input("Database username: ").strip()
    pw = getpass.getpass("Database password (hidden): ")
    return f"{scheme}://{quote_plus(user)}:{quote_plus(pw)}@{rest}"


def main() -> None:
    env = read_env()
    env["MONGODB_URI"] = build_uri()
    db = input(f"Database name [{env.get('PREGAME_DB', 'pregame_alex')}]: ").strip()
    env["PREGAME_DB"] = db or env.get("PREGAME_DB", "pregame_alex")
    key = getpass.getpass("Anthropic API key (hidden; Enter to keep the current one): ").strip()
    if key:
        env["ANTHROPIC_API_KEY"] = key
    env.setdefault("PREGAME_LLM_MODE", "live" if env.get("ANTHROPIC_API_KEY") else "fake")
    ENV.write_text("".join(f"{k}={v}\n" for k, v in env.items()), encoding="utf-8")
    print(f"Wrote {ENV} with: " + ", ".join(sorted(env)) + " (values not shown).")
    print("Next: C:/Projects/prep-harness/.venv/Scripts/python.exe C:/Projects/prep-harness/scripts/smoke_atlas.py")


if __name__ == "__main__":
    main()
