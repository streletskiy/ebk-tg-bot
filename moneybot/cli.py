import argparse
import asyncio
import json
import logging
import os
import signal
import time

import psycopg

from .codex import Codex
from .config import Settings


def main():
    parser = argparse.ArgumentParser(prog="moneybot")
    parser.add_argument("command", choices=["run", "health", "doctor", "login", "allow", "pair", "warmup", "migrate-sqlite"])
    parser.add_argument("user_id", nargs="?", type=int)
    parser.add_argument("--name", default="Владелец")
    parser.add_argument("--owner", action="store_true")
    parser.add_argument("--sqlite-path")
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = Settings()
    if args.command == "health":
        try:
            with psycopg.connect(**settings.database(), autocommit=True) as db:
                values = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('heartbeat','poll_heartbeat')"))
            healthy = all(time.time() - float(values.get(key, 0)) < 150 for key in ("heartbeat", "poll_heartbeat"))
            raise SystemExit(0 if healthy else 1)
        except (psycopg.Error, OSError, ValueError):
            raise SystemExit(1)
    if args.command == "allow":
        if not args.user_id or args.user_id <= 0:
            parser.error("positive Telegram user_id required")
        raw = json.loads(settings.contacts_path.read_text("utf-8")) if settings.contacts_path.exists() else {"owner_id": 0, "contacts": {}}
        raw["contacts"][str(args.user_id)] = {"name": args.name, "default_account_id": settings.default_account, "currency": settings.currency}
        if args.owner:
            raw["owner_id"] = args.user_id
        settings.contacts_path.parent.mkdir(parents=True, exist_ok=True)
        settings.contacts_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), "utf-8")
        settings.contacts_path.chmod(0o600)
        print("Contact saved")
        return
    try:
        if args.command == "migrate-sqlite":
            from .migration import migrate_sqlite
            source = args.sqlite_path or str(settings.data / "moneybot.sqlite3")
            print(json.dumps(migrate_sqlite(settings, source)))
            return
        asyncio.run(async_main(args.command, settings))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Exception chains from HTTP clients may contain secret-bearing request URLs.
        logging.error("COMMAND_FAILED type=%s", type(exc).__name__)
        raise SystemExit(1) from None


async def async_main(command, settings):
    if command == "pair":
        import hashlib
        import secrets
        from .telegram import Telegram
        if settings.owner:
            raise ValueError("Owner is already configured; use allow instead")
        telegram = Telegram(settings)
        try:
            me = await telegram.call("getMe")
            secret = secrets.token_urlsafe(32)
            path = settings.data / "pairing.json"
            path.write_text(json.dumps({"sha256": hashlib.sha256(secret.encode()).hexdigest(),
                                         "expires": time.time() + 7*86400}), "utf-8")
            path.chmod(0o600)
            print(f"https://t.me/{me['username']}?start={secret}")
        finally:
            await telegram.http.aclose()
        return
    if command == "run":
        from .service import Service
        service = Service(settings)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, service.stopping.set)
            except NotImplementedError:
                pass
        await service.run()
        return
    if command == "warmup":
        from faster_whisper import WhisperModel
        await asyncio.to_thread(WhisperModel, os.getenv("WHISPER_MODEL", "small"), device="cpu",
                                compute_type="int8", download_root=str(settings.data / "models"))
        print("Voice model ready")
        return
    codex = Codex(settings)
    try:
        if command == "login":
            login = await codex.login()
            print(login["verificationUrl"], flush=True)
            print(login["userCode"], flush=True)
            for _ in range(180):
                await asyncio.sleep(5)
                if codex.login_success:
                    print("Codex connected")
                    return
            raise SystemExit("Login expired")
        if command == "doctor":
            from .ezbookkeeping import EzBookkeeping
            from .telegram import Telegram
            settings.validate()
            with psycopg.connect(**settings.database(), autocommit=True) as db:
                db.execute("SELECT 1").fetchone()
            telegram = Telegram(settings)
            ez = EzBookkeeping(settings, None)
            try:
                me = await telegram.call("getMe")
                catalog = await ez.catalog()
                connected = bool(await codex.account())
                print(json.dumps({"telegram": me["username"], "contacts": len(settings.contacts),
                    "ezbookkeeping": {"accounts": len(catalog["accounts"]), "categories": len(catalog["categories"])},
                    "codex_authenticated": connected, "database": "postgresql"}, ensure_ascii=False))
                if not connected:
                    raise SystemExit(1)
            finally:
                await telegram.http.aclose()
                await ez.http.aclose()
    finally:
        await codex.close()


if __name__ == "__main__":
    main()
