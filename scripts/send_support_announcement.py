#!/usr/bin/env python3
"""
One-off announcement email: new Master Education Support portal. Russian, with
English sublines in the house bilingual card style.

Sends an INDIVIDUAL email to each real user (never all-in-one "to", so recipients
never see each other's addresses). Uses the existing Resend config + branded shell.

SAFE BY DEFAULT — three explicit modes:

    # 1. Dry run (default): count recipients, write the full list to a file, send NOTHING
    python scripts/send_support_announcement.py

    # 2. Preview: send ONE real email to yourself
    python scripts/send_support_announcement.py --test you@example.com

    # 3. Deliver: actually send to every eligible user (batched, rate-limited)
    python scripts/send_support_announcement.py --send

Recipients = active, non-trial users with a real (non-@lms.com / non-test) email.
Run it where the backend runs (has the DB + RESEND_API_KEY env), e.g.:
    docker compose exec -T backend python scripts/send_support_announcement.py
"""
from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import requests

from src.config import SessionLocal
# Import UserInDB from the aggregating models package (not src.auth.models directly):
# a direct import of src.auth.models triggers a circular import via src.models.base.
from src.models import UserInDB
from src.services.email_service import _button, _email_shell, _para, get_email_service
from src.services.sync_provision_gaps import _is_test_email

BATCH_ENDPOINT = "https://api.resend.com/emails/batch"  # up to 100 messages / request
SUPPORT_URL = "https://support.mastereducation.kz"

SUBJECT = "Master Education: новый портал поддержки"


def build_html(name: str) -> str:
    greeting = f"Здравствуйте, {name}!" if name and name.strip() else "Здравствуйте!"
    inner = f"""
      <p style="margin:0 0 16px;font-size:15px;">{greeting}</p>

      {_para(
          "У Master Education теперь есть единый портал поддержки — для вопросов по LMS, "
          "SAT/NUET и IELTS.",
          "Master Education now has a single support portal — for questions about LMS, "
          "SAT/NUET, and IELTS.",
      )}
      {_para(
          "Входите тем же аккаунтом Master Education, что и раньше, через кнопку "
          "«Продолжить с Master Education». Отдельный пароль не нужен.",
          "Sign in with your existing Master Education account, via the same “Continue "
          "with Master Education” button. No separate password needed.",
      )}
      {_para(
          "На большинство вопросов мгновенно отвечает AI-ассистент. Если нужен человек — "
          "об этом автоматически узнаёт ваш куратор.",
          "An AI assistant answers most questions instantly. If you need a person, your "
          "curator is notified automatically.",
      )}

      <div style="margin:24px 0;">{_button(SUPPORT_URL, "Открыть портал поддержки / Open support portal")}</div>
    """
    return _email_shell("🎧 Портал поддержки Master Education", inner)


TEXT = (
    "Здравствуйте!\n\n"
    "У Master Education теперь есть единый портал поддержки — для вопросов по LMS, "
    "SAT/NUET и IELTS.\n\n"
    "Входите тем же аккаунтом Master Education, что и раньше, через кнопку "
    "«Продолжить с Master Education». Отдельный пароль не нужен.\n\n"
    "На большинство вопросов мгновенно отвечает AI-ассистент. Если нужен человек — "
    "об этом автоматически узнаёт ваш куратор.\n\n"
    f"Портал поддержки: {SUPPORT_URL}\n\n"
    "Команда Master Education"
)


def eligible_recipients(db) -> list[tuple[str, str]]:
    """Active, non-trial users with a real email — deduped by lowercased address."""
    rows = (
        db.query(UserInDB.email, UserInDB.name)
        .filter(UserInDB.is_active.is_(True), UserInDB.is_trial.is_(False))
        .all()
    )
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for email, name in rows:
        e = (email or "").strip()
        key = e.lower()
        if not e or key in seen or _is_test_email(e):
            continue
        seen.add(key)
        out.append((e, name or ""))
    return out


def sender(svc) -> str:
    """Neutral cross-platform display name (this is not an LMS-only message)."""
    m = re.search(r"<([^>]+)>", svc.from_email)
    addr = m.group(1) if m else svc.from_email
    return f"Master Education <{addr}>"


def send_batch(svc, batch: list[tuple[str, str]]) -> dict:
    from_email = sender(svc)
    payload = [
        {
            "from": from_email,
            "to": [email],
            "subject": SUBJECT,
            "html": build_html(name),
            "text": TEXT,
        }
        for email, name in batch
    ]
    resp = requests.post(BATCH_ENDPOINT, json=payload, headers=svc._get_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    ap = argparse.ArgumentParser(description="Send the support-portal announcement email.")
    ap.add_argument("--test", metavar="EMAIL", help="send ONE real email to this address only")
    ap.add_argument("--send", action="store_true", help="actually send to ALL eligible users")
    ap.add_argument("--limit", type=int, default=0, help="cap recipients (0 = all); for staged sends")
    args = ap.parse_args()

    svc = get_email_service()
    if not svc.is_configured:
        print("❌ Resend not configured (RESEND_API_KEY missing). Aborting.")
        sys.exit(1)
    print(f"From: {sender(svc)}")

    if args.test:
        res = send_batch(svc, [(args.test, "")])
        print(f"✅ Test email sent to {args.test} → {res}")
        return

    db = SessionLocal()
    try:
        recipients = eligible_recipients(db)
    finally:
        db.close()

    if args.limit:
        recipients = recipients[: args.limit]

    out_file = Path(__file__).resolve().parent / "support_announcement_recipients.txt"
    payload = "\n".join(e for e, _ in recipients)
    try:
        out_file.write_text(payload, encoding="utf-8")
    except OSError:
        # In the deployed container scripts/ is a read-only bind mount; fall back to a writable dir.
        out_file = Path(tempfile.gettempdir()) / "support_announcement_recipients.txt"
        out_file.write_text(payload, encoding="utf-8")
    print(f"Eligible recipients: {len(recipients)}")
    print(f"Full list written to: {out_file}")
    print(f"Sample: {[e for e, _ in recipients[:5]]}")

    if not args.send:
        print("\nDRY RUN — nothing was sent.")
        print("Next: preview with  --test you@example.com   then deliver with  --send")
        return

    print(f"\nSending to {len(recipients)} recipients in batches of 100 …")
    sent, failed = 0, 0
    for i in range(0, len(recipients), 100):
        batch = recipients[i : i + 100]
        try:
            send_batch(svc, batch)
            sent += len(batch)
            print(f"  ✅ {sent}/{len(recipients)}")
        except Exception as exc:  # noqa: BLE001
            failed += len(batch)
            print(f"  ❌ batch {i}-{i + len(batch)} failed: {exc}")
        time.sleep(1)  # stay under Resend's request rate limit
    print(f"\nDone. Sent {sent}, failed {failed}, total {len(recipients)}.")


if __name__ == "__main__":
    main()
