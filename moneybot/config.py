import json
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Contact:
    name: str
    account: str = ""
    currency: str = "RUB"


class Settings:
    def __init__(self):
        self.data = Path(os.getenv("DATA_DIR", ".local/data")).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.contacts_path = Path(os.getenv("CONTACTS_FILE", str(self.data / "contacts.json")))
        self.token = os.getenv("TELEGRAM_TOKEN", "")
        self.ez_url = os.getenv("EZBOOKKEEPING_URL", "http://ezbookkeeping:8080").rstrip("/")
        self.ez_token_file = Path(os.getenv("EZBOOKKEEPING_TOKEN_FILE", str(self.data / "ezbookkeeping.token")))
        self.codex_home = Path(os.getenv("CODEX_HOME", str(self.data / "codex"))).resolve()
        self.codex_bin = os.getenv("CODEX_BIN", "codex")
        self.model = os.getenv("CODEX_MODEL", "gpt-6.1-sol")
        self.effort = os.getenv("CODEX_EFFORT", "medium")
        self.codex_timeout = int(os.getenv("CODEX_TIMEOUT", "180"))
        self.timezone = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
        self.default_account = os.getenv("DEFAULT_ACCOUNT_ID", "")
        self.currency = os.getenv("DEFAULT_CURRENCY", "RUB")
        self.max_media = int(os.getenv("MAX_MEDIA_MB", "15")) * 1024 * 1024
        self.max_voice = int(os.getenv("MAX_VOICE_SECONDS", "180"))
        self.max_rows = int(os.getenv("MAX_QUERY_ROWS", "10000"))
        self.retention_days = int(os.getenv("RETENTION_DAYS", "30"))
        self.owner = 0
        self.contacts: dict[int, Contact] = {}
        self.reload_contacts()

    def reload_contacts(self):
        raw = json.loads(self.contacts_path.read_text("utf-8")) if self.contacts_path.exists() else {}
        contacts = {}
        for key, value in raw.get("contacts", {}).items():
            uid = int(key)
            if uid <= 0:
                raise ValueError("Contact IDs must be positive")
            contacts[uid] = Contact(value.get("name", str(uid)), str(value.get("default_account_id", "")),
                                    value.get("currency", self.currency))
        owner = int(raw.get("owner_id", 0))
        if owner and owner not in contacts:
            raise ValueError("Owner must be in contacts")
        self.owner, self.contacts = owner, contacts

    def ez_token(self):
        return self.ez_token_file.read_text("utf-8").strip()

    def database(self):
        password_file = Path(os.getenv("PGPASSWORD_FILE", str(self.data / "postgres.password")))
        return {
            "host": os.getenv("PGHOST", "db"), "port": int(os.getenv("PGPORT", "5432")),
            "dbname": os.getenv("PGDATABASE", "ebk_tg_bot"), "user": os.getenv("PGUSER", "ebk_tg_bot"),
            "password": password_file.read_text("utf-8").strip(), "connect_timeout": 5,
            "application_name": "ebk-tg-bot", "options": "-c statement_timeout=15000 -c lock_timeout=5000",
            "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3,
        }

    def validate(self):
        pairing = self.data / "pairing.json"
        if not self.token or ((not self.contacts or not self.owner) and not pairing.exists()):
            raise ValueError("Configure Telegram token, owner and contacts before starting")
        if not self.ez_token():
            raise ValueError("ezBookkeeping token is empty")
