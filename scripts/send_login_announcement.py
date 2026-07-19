#!/usr/bin/env python3
"""
One-off announcement email: unified login + "Sign in with Google". Russian.

Sends an INDIVIDUAL email to each real user (never all-in-one "to", so recipients
never see each other's addresses). Uses the existing Resend config + branded shell.

SAFE BY DEFAULT — three explicit modes:

    # 1. Dry run (default): count recipients, write the full list to a file, send NOTHING
    python scripts/send_login_announcement.py

    # 2. Preview: send ONE real email to yourself
    python scripts/send_login_announcement.py --test you@example.com

    # 3. Deliver: actually send to every eligible user (batched, rate-limited)
    python scripts/send_login_announcement.py --send

Recipients = active, non-trial users with a real (non-@lms.com / non-test) email.
Run it where the backend runs (has the DB + RESEND_API_KEY env), e.g.:
    docker compose exec -T backend python scripts/send_login_announcement.py
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

import requests

from src.config import SessionLocal
# Import UserInDB from the aggregating models package (not src.auth.models directly):
# a direct import of src.auth.models triggers a circular import via src.models.base.
from src.models import UserInDB
from src.services.email_service import _button, _email_shell, get_email_service
from src.services.sync_provision_gaps import _is_test_email

BATCH_ENDPOINT = "https://api.resend.com/emails/batch"  # up to 100 messages / request
LOGIN_URL = "https://lms.mastereducation.kz"

SUBJECT = "Master Education: теперь можно входить через Google"


def build_html(name: str) -> str:
    greeting = f"Здравствуйте, {name}!" if name and name.strip() else "Здравствуйте!"
    p = "margin:0 0 16px;font-size:15px;color:#333333;"
    li = "margin:0 0 8px;font-size:15px;color:#333333;"
    inner = f"""
      <p style="{p}">{greeting}</p>

      <p style="{p}">У нас хорошие новости: вход в Master Education стал проще и удобнее.
      <b>Теперь вы можете входить через Google — в один клик, без пароля.</b></p>

      <p style="{p}">Все платформы Master Education (LMS, SAT / NUET, IELTS) используют
      единый вход (Single Sign-On). Мы обновили страницу входа: добавили вход через Google,
      сделали интерфейс удобнее, а сообщения об ошибках — понятнее. Интерфейс входа
      теперь доступен на русском, английском и казахском языках.</p>

      <p style="margin:0 0 12px;font-size:15px;color:#111111;font-weight:600;">Как войти через Google:</p>
      <ol style="margin:0 0 16px;padding-left:20px;">
        <li style="{li}">Откройте страницу входа в любую платформу Master Education.</li>
        <li style="{li}">Нажмите кнопку «Войти через Google».</li>
        <li style="{li}">Выберите Google-аккаунт с той же электронной почтой, что указана
        в вашем профиле Master Education.</li>
      </ol>

      <p style="{p}">Готово — вы войдёте автоматически.</p>

      <div style="margin:24px 0;">{_button(LOGIN_URL, "Войти в Master Education")}</div>

      <p style="margin:0 0 16px;font-size:14px;color:#666666;">
      <b>Важно:</b> вход через Google работает, если ваш Google-аккаунт использует ту же
      почту, что и аккаунт в Master Education. Если почта отличается — вы, как и раньше,
      можете войти по электронной почте и паролю. Ничего настраивать не нужно.</p>

      <p style="margin:0;font-size:14px;color:#666666;">
      Есть вопросы? Обращайтесь к своему куратору.</p>
    """
    return _email_shell("🔐 Единый вход в Master Education", inner)


TEXT = (
    "Здравствуйте!\n\n"
    "Вход в Master Education стал проще: теперь вы можете входить через Google — "
    "в один клик, без пароля.\n\n"
    "Все платформы Master Education (LMS, SAT/NUET, IELTS) используют единый вход (SSO). "
    "Мы обновили страницу входа и добавили вход через Google.\n\n"
    "Как войти через Google:\n"
    "1. Откройте страницу входа в любую платформу Master Education.\n"
    "2. Нажмите «Войти через Google».\n"
    "3. Выберите Google-аккаунт с той же почтой, что указана в вашем профиле.\n\n"
    "Важно: вход через Google работает, если почта Google совпадает с вашей почтой "
    "в Master Education. Иначе — входите по email и паролю, как раньше.\n\n"
    f"Войти: {LOGIN_URL}\n"
    "Есть вопросы? Обращайтесь к своему куратору.\n\n"
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
    ap = argparse.ArgumentParser(description="Send the Google-login announcement email.")
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

    out_file = Path(__file__).resolve().parent / "announcement_recipients.txt"
    out_file.write_text("\n".join(e for e, _ in recipients), encoding="utf-8")
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
