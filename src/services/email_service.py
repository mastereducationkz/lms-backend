"""
Email Service using Resend API
Configuration is loaded from environment variables.
"""
import os
import logging
from typing import List, Optional

import requests
from dotenv import load_dotenv

from src.services import email_log
from src.curator.email_policy import (
    SUPPRESSION_REASON as CURATOR_SUPPRESSION_REASON,
    is_operational_event,
)

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Load configuration from environment variables
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "noreply@mail.mastereducation.kz")
EMAIL_SENDER_NAME = os.getenv("EMAIL_SENDER_NAME", "LMS | Master Education")
DEFAULT_LMS_BASE_URL = "https://lms.mastereducation.kz"


def _get_lms_base_url() -> str:
    """Return normalized LMS base URL, resilient to empty env values."""
    raw_url = (os.getenv("LMS_URL") or "").strip()
    if not raw_url:
        return DEFAULT_LMS_BASE_URL
    return raw_url.rstrip("/")


def _build_lms_url(path: str = "") -> str:
    base_url = _get_lms_base_url()
    if not path:
        return base_url
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base_url}{path}"


def _build_homework_url(assignment_id: Optional[int] = None) -> str:
    if assignment_id is None:
        return _build_lms_url("/homework")
    return _build_lms_url(f"/homework/{assignment_id}")


class EmailService:
    """Email service for sending notifications via Resend API"""
    
    RESEND_API_URL = "https://api.resend.com/emails"
    
    def __init__(self):
        self.api_key = RESEND_API_KEY
        
        # Debug logging for environment variables
        logger.info(f"🔧 [EMAIL] Initializing EmailService")
        logger.info(f"   EMAIL_SENDER env var: '{EMAIL_SENDER}'")
        logger.info(f"   EMAIL_SENDER_NAME env var: '{EMAIL_SENDER_NAME}'")
        
        # If EMAIL_SENDER already contains name (e.g., "Name <email@domain.com>"), use as-is
        # Otherwise, combine EMAIL_SENDER_NAME and EMAIL_SENDER
        if "<" in EMAIL_SENDER and ">" in EMAIL_SENDER:
            # Already formatted as "Name <email@domain.com>"
            self.from_email = EMAIL_SENDER
            logger.info(f"   ✓ Using EMAIL_SENDER as-is (already formatted)")
        else:
            # Combine name and email
            self.from_email = f"{EMAIL_SENDER_NAME} <{EMAIL_SENDER}>"
            logger.info(f"   ✓ Combined EMAIL_SENDER_NAME + EMAIL_SENDER")
        
        logger.info(f"   📧 Final from_email: '{self.from_email}'")
        
        if not self.api_key:
            logger.warning("RESEND_API_KEY not configured. Email notifications will be disabled.")
    
    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)
    
    def _get_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
    
    @staticmethod
    def _drop_curator_recipients(emails: List[str]) -> tuple[List[str], List[str]]:
        """Split recipients into (may receive, must not). Fails **closed**.

        Opens its own short-lived session: this runs on the send path, which is called from
        many transactions and from background workers, and it must not join any of them. If
        the lookup itself fails the recipients are withheld rather than sent — an unavailable
        policy check must not become a delivered message.
        """
        from src.config import SessionLocal
        from src.curator.email_policy import curator_user_ids

        if not emails:
            return [], []
        db = SessionLocal()
        try:
            from src.schemas.models import UserInDB

            rows = (
                db.query(UserInDB.id, UserInDB.email)
                .filter(UserInDB.email.in_(emails))
                .all()
            )
            by_id = {int(uid): addr for uid, addr in rows}
            curator_addresses = {
                by_id[uid] for uid in curator_user_ids(db, list(by_id.keys())) if uid in by_id
            }
        except Exception:  # noqa: BLE001 — withhold rather than risk a leak
            logger.exception("curator email policy check failed; withholding all recipients")
            return [], list(emails)
        finally:
            db.close()
        kept = [e for e in emails if e not in curator_addresses]
        withheld = [e for e in emails if e in curator_addresses]
        return kept, withheld

    def send_email(
        self,
        to_emails: List[str],
        subject: str,
        html_content: str,
        text_content: Optional[str] = None,
        *,
        event_type: str = "other",
        recipient_user_id: Optional[int] = None,
        related_type: Optional[str] = None,
        related_id: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Send email using Resend API

        Args:
            to_emails: List of recipient email addresses
            subject: Email subject
            html_content: HTML body of the email
            text_content: Optional plain text version
            event_type: Journal slug for this kind of mail (see email_log.EVENT_TYPES)
            recipient_user_id: LMS user this is addressed to, when known
            related_type/related_id: what the mail is about (assignment, event, …)
            idempotency_key: claim this key before sending; if another process already
                holds it the send is skipped and ``None`` is returned. Single-recipient
                sends only — a shared key across a list could not be reasoned about.

        Returns:
            Response from Resend API or None if failed

        Every recipient gets one journal row, claimed *before* the HTTP call so a crash
        mid-send still leaves evidence. Journal failures are contained inside
        :mod:`src.services.email_log` and never stop the send.
        """
        logger.info(f"📧 [EMAIL] Attempting to send email: '{subject}'")
        logger.info(f"   Recipients: {to_emails}")

        if not to_emails:
            logger.warning("⚠️  [EMAIL] No recipients provided for email")
            return None

        # Filter out empty/invalid emails
        valid_emails = [e.strip() for e in to_emails if e and "@" in e]
        if not valid_emails:
            logger.warning(f"⚠️  [EMAIL] No valid email addresses provided. Input was: {to_emails}")
            return None

        if len(valid_emails) < len(to_emails):
            logger.warning(f"⚠️  [EMAIL] Filtered {len(to_emails) - len(valid_emails)} invalid emails")

        if idempotency_key and len(valid_emails) > 1:
            logger.error(
                "❌ [EMAIL] idempotency_key given with %s recipients; refusing to send",
                len(valid_emails),
            )
            return None

        # Curators and head curators are notified in-app only. Enforced *here*, at the single
        # funnel every LMS email passes through, rather than at each composer: there are at
        # least six code paths that build curator mail, one of them
        # (`send_lesson_change_curator_notification`) assembles its own HTML and never calls
        # the shared curator helper, and the next one nobody has written yet would be seventh.
        #
        # Identity and account-recovery mail is exempt by allow-list, so invitations and
        # password resets keep working for curators like anyone else.
        if is_operational_event(event_type):
            kept, withheld = self._drop_curator_recipients(valid_emails)
            for email in withheld:
                # Recorded, not silently dropped: the journal must show a withheld message
                # rather than a gap somebody has to guess about.
                email_log.finish(
                    email_log.claim(
                        event_type=event_type,
                        recipient_email=email,
                        subject=subject,
                        recipient_user_id=recipient_user_id,
                        related_type=related_type,
                        related_id=related_id,
                    ),
                    status="suppressed",
                    error=CURATOR_SUPPRESSION_REASON,
                    event_type=event_type,
                )
            if not kept:
                logger.info(
                    "📭 [EMAIL] all recipients are curators; '%s' withheld (%s)",
                    subject, CURATOR_SUPPRESSION_REASON,
                )
                return None
            valid_emails = kept

        def _journal(email: str, key: Optional[str]) -> Optional[int]:
            return email_log.claim(
                event_type=event_type,
                recipient_email=email,
                subject=subject,
                recipient_user_id=recipient_user_id,
                related_type=related_type,
                related_id=related_id,
                idempotency_key=key,
                # Handed over as sent. Whether any of it is *kept* is decided inside the
                # journal from `event_type`, not here — a credential body must not depend on
                # each of twenty-four call sites remembering to withhold it.
                html_content=html_content,
                text_content=text_content,
            )

        if not self.is_configured:
            logger.error("❌ [EMAIL] Email service not configured - RESEND_API_KEY is missing!")
            logger.error(f"   Current RESEND_API_KEY value: {self.api_key or 'None'}")
            # Recorded without the idempotency key: nothing was sent, so a later run with a
            # working key must still be allowed to claim it.
            for email in valid_emails:
                email_log.finish(
                    _journal(email, None),
                    status="suppressed",
                    error="email service not configured",
                    event_type=event_type,
                )
            return None

        claims = [_journal(email, idempotency_key) for email in valid_emails]
        if idempotency_key and claims[0] is None:
            logger.info(f"⏭️  [EMAIL] '{idempotency_key}' already claimed; not sending again")
            return None

        payload = {
            "from": self.from_email,
            "to": valid_emails,
            "subject": subject,
            "html": html_content
        }

        if text_content:
            payload["text"] = text_content

        logger.info(f"📤 [EMAIL] Sending to Resend API ({self.RESEND_API_URL})...")
        logger.info(f"   📧 From: '{payload['from']}'")
        logger.info(f"   📬 To: {payload['to']}")
        logger.info(f"   📝 Subject: '{payload['subject']}'")
        logger.debug(f"   Full payload keys: {list(payload.keys())}")

        def _close(status: str, message_id: Optional[str] = None, error: object = None) -> None:
            for claim_id in claims:
                email_log.finish(
                    claim_id,
                    status=status,
                    provider_message_id=message_id,
                    error=error,
                    event_type=event_type,
                )

        try:
            response = requests.post(
                self.RESEND_API_URL,
                json=payload,
                headers=self._get_headers(),
                timeout=10
            )

            logger.info(f"📥 [EMAIL] Resend API response status: {response.status_code}")

            response.raise_for_status()

            response_data = response.json()
            logger.info(f"✅ [EMAIL] Successfully sent to {len(valid_emails)} recipient(s)")
            logger.debug(f"   Response data: {response_data}")

            _close("sent", message_id=(response_data or {}).get("id"))
            return response_data
        except requests.exceptions.Timeout as e:
            logger.error("❌ [EMAIL] Request timed out after 10 seconds")
            _close("failed", error=e)
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ [EMAIL] Failed to send email: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"   Response status: {e.response.status_code}")
                logger.error(f"   Response body: {e.response.text}")
            _close("failed", error=e)
            return None
        except Exception as e:
            # A malformed success body (json() raising) would otherwise leave the row stuck
            # at "queued" forever with no explanation.
            logger.error(f"❌ [EMAIL] Unexpected error sending email: {e}")
            _close("failed", error=e)
            return None


# Singleton instance
_email_service: Optional[EmailService] = None


def get_email_service() -> EmailService:
    """Get or create the email service singleton"""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service


# ── Account emails (invite / password) — bilingual RU + EN ────────────────────
# Shared layout matches the lesson-reminder email (500px card + logo header).

_LOGO_SVG = (
    '<svg version="1.0" xmlns="http://www.w3.org/2000/svg" width="40px" height="40px" '
    'viewBox="0 0 150 150" preserveAspectRatio="xMidYMid meet" style="vertical-align:middle;">'
    '<g transform="translate(0,150) scale(0.1,-0.1)" fill="#2563eb" stroke="none">'
    '<path d="M556 1221 c-8 -13 85 -232 101 -238 22 -9 38 12 62 82 13 36 26 67 30 69 4 3 20 -29 36 -70 29 -70 56 -99 75 -77 19 22 90 227 81 236 -19 19 -38 -3 -65 -77 -16 -42 -31 -76 -36 -76 -4 0 -21 33 -38 73 -25 59 -34 72 -52 72 -19 0 -28 -13 -53 -80 l-30 -79 -14 29 c-7 17 -24 56 -38 88 -23 52 -45 71 -59 48z"/>'
    '<path d="M420 1134 c0 -9 23 -43 50 -76 28 -33 50 -64 50 -70 0 -5 -12 -7 -27 -4 -86 16 -136 18 -144 5 -13 -21 -12 -24 42 -89 28 -34 49 -63 47 -66 -3 -2 -44 1 -92 8 -65 8 -90 8 -99 -1 -8 -8 -8 -14 0 -22 12 -12 227 -43 248 -35 25 9 17 35 -32 97 -25 33 -44 61 -42 64 3 2 34 0 69 -5 90 -13 98 -13 105 10 5 15 -12 42 -67 110 -69 84 -108 111 -108 74z"/>'
    '<path d="M972 1054 c-61 -81 -70 -98 -61 -115 8 -15 17 -19 42 -14 18 3 53 9 80 14 29 5 47 5 47 -1 0 -5 -20 -36 -45 -68 -49 -63 -52 -72 -32 -89 10 -8 44 -5 128 9 63 11 115 20 117 20 1 0 2 10 2 21 0 20 -4 21 -47 15 -27 -4 -70 -10 -98 -13 l-49 -6 53 66 c39 49 51 72 46 87 -7 23 -6 23 -96 9 -38 -7 -72 -9 -75 -6 -4 3 17 35 45 70 53 67 63 97 34 97 -11 0 -48 -39 -91 -96z"/>'
    '<path d="M358 712 c-100 -17 -132 -32 -111 -53 8 -8 34 -7 97 3 47 8 86 11 86 7 0 -5 -20 -35 -45 -68 -49 -64 -52 -73 -32 -90 10 -8 34 -7 91 3 90 16 89 17 21 -73 -43 -56 -53 -91 -26 -91 10 0 91 97 139 166 18 26 19 34 9 48 -12 16 -20 16 -72 7 -113 -21 -114 -20 -54 55 42 53 50 70 42 83 -14 22 -32 22 -145 3z"/>'
    '<path d="M997 713 c-15 -14 -5 -37 38 -90 25 -30 45 -58 45 -62 0 -4 -36 -3 -81 3 -66 7 -83 7 -90 -5 -5 -8 -7 -20 -4 -27 10 -26 140 -181 153 -182 31 -1 21 33 -31 97 -31 37 -52 69 -47 71 6 2 42 -1 81 -7 53 -9 75 -9 85 0 21 17 18 27 -24 78 -75 92 -74 84 -12 77 30 -4 75 -10 98 -13 42 -5 44 -4 40 18 -3 22 -10 25 -93 36 -107 14 -149 15 -158 6z"/>'
    '<path d="M630 498 c-35 -82 -77 -205 -72 -216 11 -31 37 -2 66 76 17 45 33 82 36 82 3 0 19 -34 35 -75 27 -68 32 -75 55 -73 20 3 29 15 50 70 15 37 29 70 32 73 3 3 21 -31 40 -77 33 -79 59 -106 71 -75 3 7 -16 63 -42 123 -36 85 -52 110 -68 112 -17 3 -25 -6 -41 -50 -11 -29 -25 -66 -32 -83 l-11 -30 -36 83 c-38 87 -63 106 -83 60z"/>'
    '</g></svg>'
)


def _email_shell(emoji_title: str, inner_html: str) -> str:
    return f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  </head>
  <body style="margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background-color:#ffffff;color:#333333;line-height:1.5;">
    <div style="max-width:500px;margin:40px auto;padding:20px;">
      <div style="margin-bottom:32px;">
        <h1 style="margin:0;font-size:20px;font-weight:600;color:#111111;">{emoji_title}</h1>
        <div style="margin-top:16px;">
          {_LOGO_SVG}
          <span style="display:inline-block;vertical-align:middle;margin-left:8px;font-size:14px;color:#666666;font-weight:500;">Master Education LMS</span>
        </div>
      </div>
      <div style="margin-bottom:32px;">
        {inner_html}
      </div>
      <div style="border-top:1px solid #e5e7eb;padding-top:20px;">
        <p style="margin:0;font-size:12px;color:#999999;">
          Master Education<br />
          <a href="{_get_lms_base_url()}" style="color:#2563eb;text-decoration:none;">lms.mastereducation.kz</a>
        </p>
      </div>
    </div>
  </body>
</html>"""


def _button(href: str, label: str) -> str:
    return (
        f'<a href="{href}" style="display:inline-block;background-color:#2563eb;color:#ffffff;'
        f'padding:10px 20px;text-decoration:none;border-radius:4px;font-size:14px;font-weight:500;">{label}</a>'
    )


def _credentials_block(login_email: str, password: str) -> str:
    row = (
        '<div style="margin-bottom:12px;"><div style="font-size:13px;color:#666666;margin-bottom:4px;">{label}</div>'
        '<div style="font-size:15px;font-weight:600;color:#111111;font-family:monospace;">{value}</div></div>'
    )
    rows = (
        row.format(label="Логин / Login", value=login_email)
        + row.format(label="Пароль / Password", value=password).replace("margin-bottom:12px;", "")
    )
    return (
        '<div style="background-color:#f8fafc;padding:16px;border-radius:6px;border:1px solid #e5e7eb;margin:8px 0 24px;">'
        f'{rows}</div>'
    )


def _para(ru: str, en: str = "") -> str:
    en_html = f'<p style="margin:0 0 16px;font-size:14px;color:#666666;">{en}</p>' if en else ""
    return f'<p style="margin:0 0 8px;font-size:15px;">{ru}</p>{en_html}'


def build_invite_email(
    name: str, login_email: str, password: str,
    access_until: Optional[str] = None,
) -> dict:
    """Render the invite email (subject/html/text) — pure, so it is unit-testable.

    Two audiences, one signature. A **regular** student invite (access_until=None) is
    backed by the shared Master Education account (Zitadel), so it tells the recipient to
    sign in via the "Continue with Master Education" button. A **trial** invite
    (access_until set) is an LMS-only account with NO Zitadel identity — that button can
    never authenticate them — so it instead tells the prospect to sign in with email +
    password directly on the LMS login page, and states the trial deadline.
    """
    base_url = _get_lms_base_url()
    greeting = name or "студент"
    is_trial = access_until is not None

    if is_trial:
        header = "🎓 Пробный доступ / Trial access"
        subject = "Ваш пробный доступ к Master Education / Your Master Education trial access"
        intro = _para(
            "Для вас открыт пробный доступ к платформе LMS Master Education. Войдите по email "
            "и паролю ниже прямо на странице входа LMS — кнопка «Продолжить с Master Education» "
            "для пробного доступа не используется. Данные для входа:",
            "Trial access to the Master Education LMS has been opened for you. Sign in with the "
            "email and password below directly on the LMS sign-in page — the “Continue with "
            "Master Education” button is not used for trial access. Your credentials:",
        )
        closing = _para(
            "Введите email и пароль на странице входа LMS. Пробный доступ ограничен по времени.",
            "Enter the email and password on the LMS sign-in page. Trial access is time-limited.",
        )
        text_intro = (
            f"Здравствуйте, {greeting}! Для вас открыт пробный доступ к LMS Master Education. "
            "Войдите по email и паролю ниже прямо на странице входа LMS "
            "(без кнопки «Продолжить с Master Education»).\n"
        )
    else:
        header = "🎓 Добро пожаловать / Welcome"
        subject = "Ваш аккаунт Master Education готов / Your Master Education account is ready"
        intro = _para(
            "Для вас создан единый аккаунт Master Education. С ним вы входите во все платформы "
            "Master Education (LMS, SAT/NUET, IELTS и другие) — одним аккаунтом, через кнопку "
            "«Продолжить с Master Education». Данные для входа:",
            "Your single Master Education account is ready. Use it to sign in to every Master "
            "Education platform (LMS, SAT/NUET, IELTS and more) with one account, via the "
            "“Continue with Master Education” button. Your credentials:",
        )
        closing = _para(
            "На странице входа нажмите «Продолжить с Master Education» и войдите с данными выше. "
            "Рекомендуем сменить пароль после первого входа.",
            "On the sign-in page, choose “Continue with Master Education” and log in with the "
            "details above. We recommend changing your password after your first sign-in.",
        )
        text_intro = (
            f"Здравствуйте, {greeting}! Для вас создан единый аккаунт Master Education — "
            "он работает во всех платформах через «Продолжить с Master Education».\n"
        )

    trial_notice = (
        _para(
            f"Ваш пробный доступ действует до {access_until}.",
            f"Your trial access is available until {access_until}.",
        )
        if access_until else ""
    )
    inner = (
        f'<p style="margin:0 0 16px;font-size:15px;">Здравствуйте, <strong>{greeting}</strong>!</p>'
        + intro
        + _credentials_block(login_email, password)
        + trial_notice
        + f'<div style="margin-bottom:24px;">{_button(base_url, "Войти / Sign in")}</div>'
        + closing
    )
    trial_text = f"Пробный доступ до / Trial access until: {access_until}\n" if access_until else ""
    text_content = (
        text_intro
        + f"Логин/Login: {login_email}\nПароль/Password: {password}\n"
        + trial_text
        + f"Войти / Sign in: {base_url}\n"
    )
    return {
        "subject": subject,
        "html": _email_shell(header, inner),
        "text": text_content,
    }


def send_invite_email(
    to_email: str, name: str, login_email: str, password: str,
    access_until: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Optional[dict]:
    """Welcome/invite email with platform link and login credentials (students).

    access_until, when given, marks this as a trial invite: the copy switches to
    direct LMS email/password login (no SSO button) and states the trial deadline.
    """
    content = build_invite_email(name, login_email, password, access_until)
    return get_email_service().send_email(
        to_emails=[to_email],
        subject=content["subject"],
        html_content=content["html"],
        text_content=content["text"],
        event_type="trial_invite" if access_until else "invite",
        recipient_user_id=user_id,
    )


def send_password_changed_email(
    to_email: str, name: str, new_password: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Optional[dict]:
    """Password-changed email. If new_password is given (admin-set), include it + login link."""
    base_url = _get_lms_base_url()
    greeting = (", " + name) if name else ""
    if new_password:
        inner = (
            f'<p style="margin:0 0 16px;font-size:15px;">Здравствуйте{greeting}!</p>'
            + _para(
                "Администратор изменил пароль вашего аккаунта Master Education. Этот единый аккаунт "
                "работает во всех платформах Master Education через кнопку «Продолжить с Master Education». "
                "Новые данные для входа:",
                "An administrator changed the password of your Master Education account. This single account "
                "signs you in to every Master Education platform via “Continue with Master Education”. New credentials:",
            )
            + _credentials_block(to_email, new_password)
            + f'<div style="margin-bottom:24px;">{_button(base_url, "Войти / Sign in")}</div>'
            + '<p style="margin:0;font-size:14px;color:#666666;">Рекомендуем сменить пароль после входа.<br/>We recommend changing it after you sign in.</p>'
        )
        text = f"Пароль вашего аккаунта Master Education изменён администратором (работает во всех платформах через «Продолжить с Master Education»). Логин/Login: {to_email} Пароль/Password: {new_password} {base_url}"
    else:
        reset_url = _build_lms_url("/forgot-password")
        inner = (
            f'<p style="margin:0 0 16px;font-size:15px;">Здравствуйте{greeting}!</p>'
            + _para(
                "Пароль вашего аккаунта был успешно изменён.",
                "Your account password was successfully changed.",
            )
            + f'<p style="margin:0;font-size:13px;color:#999999;">Если это были не вы — '
            f'<a href="{reset_url}" style="color:#2563eb;">сбросьте пароль</a>.<br/>'
            f'If this wasn\'t you, reset your password immediately.</p>'
        )
        text = "Пароль вашего аккаунта был изменён. / Your account password was changed."
    return get_email_service().send_email(
        to_emails=[to_email],
        subject="Ваш пароль изменён / Your password was changed",
        html_content=_email_shell("🔑 Пароль изменён / Password changed", inner),
        text_content=text,
        event_type="password_changed",
        recipient_user_id=user_id,
    )


def send_password_reset_email(
    to_email: str, name: str, reset_url: str, user_id: Optional[int] = None,
) -> Optional[dict]:
    """Self-service password reset link (valid 1 hour)."""
    greeting = (", " + name) if name else ""
    inner = (
        f'<p style="margin:0 0 16px;font-size:15px;">Здравствуйте{greeting}!</p>'
        + _para(
            "Мы получили запрос на сброс пароля. Нажмите кнопку, чтобы задать новый:",
            "We received a request to reset your password. Click below to set a new one:",
        )
        + f'<div style="margin:8px 0 24px;">{_button(reset_url, "Сбросить пароль / Reset password")}</div>'
        + '<p style="margin:0;font-size:13px;color:#999999;">Ссылка действительна 1 час. Если вы не запрашивали сброс — проигнорируйте это письмо.<br/>This link is valid for 1 hour. If you didn\'t request this, ignore this email.</p>'
    )
    return get_email_service().send_email(
        to_emails=[to_email],
        subject="Восстановление пароля / Reset your password",
        html_content=_email_shell("🔒 Сброс пароля / Reset password", inner),
        text_content=f"Сброс пароля / Reset your password: {reset_url} (1 час / 1 hour)",
        event_type="password_reset",
        recipient_user_id=user_id,
    )


def send_homework_notification(
    student_emails: List[str],
    assignment_title: str,
    course_name: str,
    due_date: str,
    action: str = "created",
    assignment_id: Optional[int] = None,
) -> Optional[dict]:
    """
    Send notification about homework creation or update

    Args:
        student_emails: List of student email addresses
        assignment_title: Title of the assignment
        course_name: Name of the course
        due_date: Due date as formatted string
        action: Either "created" or "updated"

    Returns:
        The last provider response, or None if nothing was accepted.

    One message **per student**. This used to hand Resend the whole class in a single
    ``to:`` list, which put every classmate's address in every classmate's inbox — a
    standing privacy leak on the largest mailing the LMS does. The fan-out lives here
    rather than at the two call sites so no future caller can reintroduce it.
    """
    # No is_configured guard here (nor in the siblings below): send_email owns that
    # decision and records a `suppressed` row for it. Returning early would make "mail is
    # switched off" the one outcome the journal cannot show, which is the question it
    # exists to answer.
    service = get_email_service()

    action_text = "New Homework" if action == "created" else "Homework Updated"
    subject = f"{action_text}: {assignment_title}"
    
    verb = "has been created" if action == "created" else "has been updated"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>{subject}</title>
      </head>
      <body
        style="
          margin: 0;
          padding: 0;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
            Helvetica, Arial, sans-serif;
          background-color: #ffffff;
          color: #333333;
          line-height: 1.5;
        "
      >
        <div style="max-width: 500px; margin: 40px auto; padding: 20px">
          <!-- Header -->
          <div style="margin-bottom: 32px">
            <h1
              style="margin: 0; font-size: 20px; font-weight: 600; color: #111111"
            >
              {action_text}
            </h1>
            <div style="margin-top: 16px;">
                <svg version="1.0" xmlns="http://www.w3.org/2000/svg" width="40px" height="40px" viewBox="0 0 150 150" preserveAspectRatio="xMidYMid meet" style="vertical-align: middle;">
                    <g transform="translate(0,150) scale(0.1,-0.1)" fill="#2563eb" stroke="none">
                        <path d="M556 1221 c-8 -13 85 -232 101 -238 22 -9 38 12 62 82 13 36 26 67 30 69 4 3 20 -29 36 -70 29 -70 56 -99 75 -77 19 22 90 227 81 236 -19 19 -38 -3 -65 -77 -16 -42 -31 -76 -36 -76 -4 0 -21 33 -38 73 -25 59 -34 72 -52 72 -19 0 -28 -13 -53 -80 l-30 -79 -14 29 c-7 17 -24 56 -38 88 -23 52 -45 71 -59 48z"/>
                        <path d="M420 1134 c0 -9 23 -43 50 -76 28 -33 50 -64 50 -70 0 -5 -12 -7 -27 -4 -86 16 -136 18 -144 5 -13 -21 -12 -24 42 -89 28 -34 49 -63 47 -66 -3 -2 -44 1 -92 8 -65 8 -90 8 -99 -1 -8 -8 -8 -14 0 -22 12 -12 227 -43 248 -35 25 9 17 35 -32 97 -25 33 -44 61 -42 64 3 2 34 0 69 -5 90 -13 98 -13 105 10 5 15 -12 42 -67 110 -69 84 -108 111 -108 74z"/>
                        <path d="M972 1054 c-61 -81 -70 -98 -61 -115 8 -15 17 -19 42 -14 18 3 53 9 80 14 29 5 47 5 47 -1 0 -5 -20 -36 -45 -68 -49 -63 -52 -72 -32 -89 10 -8 44 -5 128 9 63 11 115 20 117 20 1 0 2 10 2 21 0 20 -4 21 -47 15 -27 -4 -70 -10 -98 -13 l-49 -6 53 66 c39 49 51 72 46 87 -7 23 -6 23 -96 9 -38 -7 -72 -9 -75 -6 -4 3 17 35 45 70 53 67 63 97 34 97 -11 0 -48 -39 -91 -96z"/>
                        <path d="M358 712 c-100 -17 -132 -32 -111 -53 8 -8 34 -7 97 3 47 8 86 11 86 7 0 -5 -20 -35 -45 -68 -49 -64 -52 -73 -32 -90 10 -8 34 -7 91 3 90 16 89 17 21 -73 -43 -56 -53 -91 -26 -91 10 0 91 97 139 166 18 26 19 34 9 48 -12 16 -20 16 -72 7 -113 -21 -114 -20 -54 55 42 53 50 70 42 83 -14 22 -32 22 -145 3z"/>
                        <path d="M997 713 c-15 -14 -5 -37 38 -90 25 -30 45 -58 45 -62 0 -4 -36 -3 -81 3 -66 7 -83 7 -90 -5 -5 -8 -7 -20 -4 -27 10 -26 140 -181 153 -182 31 -1 21 33 -31 97 -31 37 -52 69 -47 71 6 2 42 -1 81 -7 53 -9 75 -9 85 0 21 17 18 27 -24 78 -75 92 -74 84 -12 77 30 -4 75 -10 98 -13 42 -5 44 -4 40 18 -3 22 -10 25 -93 36 -107 14 -149 15 -158 6z"/>
                        <path d="M630 498 c-35 -82 -77 -205 -72 -216 11 -31 37 -2 66 76 17 45 33 82 36 82 3 0 19 -34 35 -75 27 -68 32 -75 55 -73 20 3 29 15 50 70 15 37 29 70 32 73 3 3 21 -31 40 -77 33 -79 59 -106 71 -75 3 7 -16 63 -42 123 -36 85 -52 110 -68 112 -17 3 -25 -6 -41 -50 -11 -29 -25 -66 -32 -83 l-11 -30 -36 83 c-38 87 -63 106 -83 60z"/>
                    </g>
                </svg>
                <span style="display: inline-block; vertical-align: middle; margin-left: 8px; font-size: 14px; color: #666666; font-weight: 500;">Master Education LMS</span>
            </div>
          </div>
    
          <!-- Content -->
          <div style="margin-bottom: 32px">
            <p style="margin: 0 0 16px; font-size: 15px">Hello,</p>
            <p style="margin: 0 0 24px; font-size: 15px">
              A homework assignment <strong>{assignment_title}</strong> 
              for course <strong>{course_name}</strong> {verb}.
            </p>
    
            <div
              style="
                background-color: #f9fafb;
                padding: 16px;
                border-radius: 6px;
                border: 1px solid #e5e7eb;
                margin-bottom: 24px;
              "
            >
              <div style="font-size: 14px; margin-bottom: 4px; color: #666666">
                Due Date
              </div>
              <div style="font-size: 15px; font-weight: 500; color: #111111">
                {due_date}
              </div>
            </div>
    
            <p style="margin: 0; font-size: 15px">
              Please submit your work before the deadline to receive full credit.
            </p>
          </div>
    
          <!-- Action -->
          <div style="margin-bottom: 40px">
            <a
              href="{_build_homework_url(assignment_id)}"
              style="
                display: inline-block;
                background-color: #2563eb;
                color: #ffffff;
                padding: 10px 20px;
                text-decoration: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: 500;
              "
              >View Assignment</a
            >
          </div>
    
          <!-- Footer -->
          <div style="border-top: 1px solid #e5e7eb; padding-top: 20px">
            <p style="margin: 0; font-size: 12px; color: #999999">
              Master Education<br />
              You are receiving this email because you are enrolled in {course_name}.
            </p>
          </div>
        </div>
      </body>
    </html>
    """
    
    text_content = f"""
    {action_text}: {assignment_title}
    
    A homework assignment "{assignment_title}" for course "{course_name}" {verb}.
    
    Due Date: {due_date}
    
    Please log in to the LMS to view details and submit your work.
    
    Best regards,
    Master Education Team
    """

    event_type = "homework_new" if action == "created" else "homework_updated"
    last_result: Optional[dict] = None
    for recipient in student_emails:
        result = service.send_email(
            [recipient],
            subject,
            html_content,
            text_content,
            event_type=event_type,
            related_type="assignment",
            related_id=assignment_id,
        )
        if result is not None:
            last_result = result
    return last_result


def send_submission_graded_notification(
    student_email: str,
    assignment_title: str,
    course_name: str,
    score: int,
    max_score: int,
    feedback: Optional[str] = None,
    assignment_id: Optional[int] = None,
) -> Optional[dict]:
    """
    Send notification when a submission is graded
    
    Args:
        student_email: Student's email address
        assignment_title: Title of the assignment
        course_name: Name of the course
        score: The score received
        max_score: The maximum possible score
        feedback: Optional feedback from the teacher
        
    Returns:
        Response from email API or None
    """
    service = get_email_service()

    subject = f"Graded: {assignment_title}"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>{subject}</title>
      </head>
      <body
        style="
          margin: 0;
          padding: 0;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
            Helvetica, Arial, sans-serif;
          background-color: #ffffff;
          color: #333333;
          line-height: 1.5;
        "
      >
        <div style="max-width: 500px; margin: 40px auto; padding: 20px">
          <!-- Header -->
          <div style="margin-bottom: 32px">
            <h1
              style="margin: 0; font-size: 20px; font-weight: 600; color: #111111"
            >
              Assignment Graded
            </h1>
            <div style="margin-top: 16px;">
                <svg version="1.0" xmlns="http://www.w3.org/2000/svg" width="40px" height="40px" viewBox="0 0 150 150" preserveAspectRatio="xMidYMid meet" style="vertical-align: middle;">
                    <g transform="translate(0,150) scale(0.1,-0.1)" fill="#2563eb" stroke="none">
                        <path d="M556 1221 c-8 -13 85 -232 101 -238 22 -9 38 12 62 82 13 36 26 67 30 69 4 3 20 -29 36 -70 29 -70 56 -99 75 -77 19 22 90 227 81 236 -19 19 -38 -3 -65 -77 -16 -42 -31 -76 -36 -76 -4 0 -21 33 -38 73 -25 59 -34 72 -52 72 -19 0 -28 -13 -53 -80 l-30 -79 -14 29 c-7 17 -24 56 -38 88 -23 52 -45 71 -59 48z"/>
                        <path d="M420 1134 c0 -9 23 -43 50 -76 28 -33 50 -64 50 -70 0 -5 -12 -7 -27 -4 -86 16 -136 18 -144 5 -13 -21 -12 -24 42 -89 28 -34 49 -63 47 -66 -3 -2 -44 1 -92 8 -65 8 -90 8 -99 -1 -8 -8 -8 -14 0 -22 12 -12 227 -43 248 -35 25 9 17 35 -32 97 -25 33 -44 61 -42 64 3 2 34 0 69 -5 90 -13 98 -13 105 10 5 15 -12 42 -67 110 -69 84 -108 111 -108 74z"/>
                        <path d="M972 1054 c-61 -81 -70 -98 -61 -115 8 -15 17 -19 42 -14 18 3 53 9 80 14 29 5 47 5 47 -1 0 -5 -20 -36 -45 -68 -49 -63 -52 -72 -32 -89 10 -8 44 -5 128 9 63 11 115 20 117 20 1 0 2 10 2 21 0 20 -4 21 -47 15 -27 -4 -70 -10 -98 -13 l-49 -6 53 66 c39 49 51 72 46 87 -7 23 -6 23 -96 9 -38 -7 -72 -9 -75 -6 -4 3 17 35 45 70 53 67 63 97 34 97 -11 0 -48 -39 -91 -96z"/>
                        <path d="M358 712 c-100 -17 -132 -32 -111 -53 8 -8 34 -7 97 3 47 8 86 11 86 7 0 -5 -20 -35 -45 -68 -49 -64 -52 -73 -32 -90 10 -8 34 -7 91 3 90 16 89 17 21 -73 -43 -56 -53 -91 -26 -91 10 0 91 97 139 166 18 26 19 34 9 48 -12 16 -20 16 -72 7 -113 -21 -114 -20 -54 55 42 53 50 70 42 83 -14 22 -32 22 -145 3z"/>
                        <path d="M997 713 c-15 -14 -5 -37 38 -90 25 -30 45 -58 45 -62 0 -4 -36 -3 -81 3 -66 7 -83 7 -90 -5 -5 -8 -7 -20 -4 -27 10 -26 140 -181 153 -182 31 -1 21 33 -31 97 -31 37 -52 69 -47 71 6 2 42 -1 81 -7 53 -9 75 -9 85 0 21 17 18 27 -24 78 -75 92 -74 84 -12 77 30 -4 75 -10 98 -13 42 -5 44 -4 40 18 -3 22 -10 25 -93 36 -107 14 -149 15 -158 6z"/>
                        <path d="M630 498 c-35 -82 -77 -205 -72 -216 11 -31 37 -2 66 76 17 45 33 82 36 82 3 0 19 -34 35 -75 27 -68 32 -75 55 -73 20 3 29 15 50 70 15 37 29 70 32 73 3 3 21 -31 40 -77 33 -79 59 -106 71 -75 3 7 -16 63 -42 123 -36 85 -52 110 -68 112 -17 3 -25 -6 -41 -50 -11 -29 -25 -66 -32 -83 l-11 -30 -36 83 c-38 87 -63 106 -83 60z"/>
                    </g>
                </svg>
                <span style="display: inline-block; vertical-align: middle; margin-left: 8px; font-size: 14px; color: #666666; font-weight: 500;">Master Education LMS</span>
            </div>
          </div>
    
          <!-- Content -->
          <div style="margin-bottom: 32px">
            <p style="margin: 0 0 16px; font-size: 15px">Hello,</p>
            <p style="margin: 0 0 24px; font-size: 15px">
              Your assignment <strong>{assignment_title}</strong> 
              for course <strong>{course_name}</strong> has been graded.
            </p>
    
            <div
              style="
                background-color: #f9fafb;
                padding: 16px;
                border-radius: 6px;
                border: 1px solid #e5e7eb;
                margin-bottom: 24px;
              "
            >
              <div style="font-size: 14px; margin-bottom: 4px; color: #666666">
                Score
              </div>
              <div style="font-size: 24px; font-weight: 600; color: #111111">
                {score} <span style="font-size: 16px; font-weight: 400; color: #666666">/ {max_score}</span>
              </div>
              
              {f'<div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e5e7eb;"><div style="font-size: 14px; margin-bottom: 4px; color: #666666">Teacher Feedback</div><div style="font-size: 15px; color: #111111; white-space: pre-wrap;">{feedback}</div></div>' if feedback else ''}
            </div>
    
            <p style="margin: 0; font-size: 15px">
              Log in to the LMS to review the full details and feedback.
            </p>
          </div>
    
          <!-- Action -->
          <div style="margin-bottom: 40px">
            <a
              href="{_build_homework_url(assignment_id)}"
              style="
                display: inline-block;
                background-color: #2563eb;
                color: #ffffff;
                padding: 10px 20px;
                text-decoration: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: 500;
              "
              >View Grade</a
            >
          </div>
    
          <!-- Footer -->
          <div style="border-top: 1px solid #e5e7eb; padding-top: 20px">
            <p style="margin: 0; font-size: 12px; color: #999999">
              Master Education<br />
              You are receiving this email because you are enrolled in {course_name}.
            </p>
          </div>
        </div>
      </body>
    </html>
    """
    
    text_content = f"""
    Graded: {assignment_title}
    
    Your assignment "{assignment_title}" for course "{course_name}" has been graded.
    
    Score: {score} / {max_score}
    
    {f"Feedback: {feedback}" if feedback else ""}
    
    Please log in to the LMS to view details.

    Best regards,
    Master Education Team
    """

    return service.send_email(
        [student_email], subject, html_content, text_content,
        event_type="submission_graded",
        related_type="assignment",
        related_id=assignment_id,
    )


def reminder_idempotency_key(event_id: Optional[int], user_id: Optional[int]) -> Optional[str]:
    """The claim key for one lesson reminder to one person.

    The scheduler's in-memory set forgets everything on restart, and production runs the
    reminder in its own container: without a durable key, a deploy inside the 28–32 minute
    window re-mails the whole cohort. Keyed on the event rather than the datetime so a
    rescheduled lesson does not silently earn a second reminder.
    """
    if event_id is None or user_id is None:
        return None
    return f"lesson-reminder:{event_id}:{user_id}"


def send_lesson_reminder_notification(
    to_email: str,
    recipient_name: str,
    lesson_title: str,
    lesson_datetime: str,
    group_name: str,
    role: str = "student",
    *,
    event_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> Optional[dict]:
    """
    Send email reminder about upcoming lesson (30 minutes before)

    Args:
        to_email: Recipient email address
        recipient_name: Name of the recipient
        lesson_title: Title of the lesson
        lesson_datetime: Formatted datetime string of the lesson
        group_name: Name of the group
        role: Role of the recipient (student/teacher)
        event_id/user_id: identify this reminder so it is sent at most once — see
            :func:`reminder_idempotency_key`. Without both, no claim is made and the
            caller's own deduplication is all that protects the recipient.

    Returns:
        Response from email API or None
    """
    logger.info(f"📧 [REMINDER] Attempting to send lesson reminder to {to_email} (role: {role})")
    logger.info(f"   📚 Lesson: '{lesson_title}' | 👥 Group: '{group_name}' | ⏰ Time: {lesson_datetime}")
    
    service = get_email_service()

    # Customize content based on role
    if role == "teacher":
        subject = f"Reminder: Lesson in 30 minutes - {lesson_title}"
        greeting = "Dear Teacher,"
        message = "This is a reminder that you have a lesson starting in 30 minutes."
        action_text = "Please prepare your materials and be ready to start the lesson."
    else:  # student
        subject = f"Reminder: Lesson in 30 minutes - {lesson_title}"
        greeting = f"Hello, {recipient_name}!"
        message = "This is a reminder that your lesson is starting in 30 minutes."
        action_text = "Don't forget to join on time and be prepared!"
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>{subject}</title>
      </head>
      <body
        style="
          margin: 0;
          padding: 0;
          font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
            Helvetica, Arial, sans-serif;
          background-color: #ffffff;
          color: #333333;
          line-height: 1.5;
        "
      >
        <div style="max-width: 500px; margin: 40px auto; padding: 20px">
          <!-- Header -->
          <div style="margin-bottom: 32px">
            <h1
              style="margin: 0; font-size: 20px; font-weight: 600; color: #111111"
            >
              📚 Lesson Reminder
            </h1>
            <div style="margin-top: 16px;">
                <svg version="1.0" xmlns="http://www.w3.org/2000/svg" width="40px" height="40px" viewBox="0 0 150 150" preserveAspectRatio="xMidYMid meet" style="vertical-align: middle;">
                    <g transform="translate(0,150) scale(0.1,-0.1)" fill="#2563eb" stroke="none">
                        <path d="M556 1221 c-8 -13 85 -232 101 -238 22 -9 38 12 62 82 13 36 26 67 30 69 4 3 20 -29 36 -70 29 -70 56 -99 75 -77 19 22 90 227 81 236 -19 19 -38 -3 -65 -77 -16 -42 -31 -76 -36 -76 -4 0 -21 33 -38 73 -25 59 -34 72 -52 72 -19 0 -28 -13 -53 -80 l-30 -79 -14 29 c-7 17 -24 56 -38 88 -23 52 -45 71 -59 48z"/>
                        <path d="M420 1134 c0 -9 23 -43 50 -76 28 -33 50 -64 50 -70 0 -5 -12 -7 -27 -4 -86 16 -136 18 -144 5 -13 -21 -12 -24 42 -89 28 -34 49 -63 47 -66 -3 -2 -44 1 -92 8 -65 8 -90 8 -99 -1 -8 -8 -8 -14 0 -22 12 -12 227 -43 248 -35 25 9 17 35 -32 97 -25 33 -44 61 -42 64 3 2 34 0 69 -5 90 -13 98 -13 105 10 5 15 -12 42 -67 110 -69 84 -108 111 -108 74z"/>
                        <path d="M972 1054 c-61 -81 -70 -98 -61 -115 8 -15 17 -19 42 -14 18 3 53 9 80 14 29 5 47 5 47 -1 0 -5 -20 -36 -45 -68 -49 -63 -52 -72 -32 -89 10 -8 44 -5 128 9 63 11 115 20 117 20 1 0 2 10 2 21 0 20 -4 21 -47 15 -27 -4 -70 -10 -98 -13 l-49 -6 53 66 c39 49 51 72 46 87 -7 23 -6 23 -96 9 -38 -7 -72 -9 -75 -6 -4 3 17 35 45 70 53 67 63 97 34 97 -11 0 -48 -39 -91 -96z"/>
                        <path d="M358 712 c-100 -17 -132 -32 -111 -53 8 -8 34 -7 97 3 47 8 86 11 86 7 0 -5 -20 -35 -45 -68 -49 -64 -52 -73 -32 -90 10 -8 34 -7 91 3 90 16 89 17 21 -73 -43 -56 -53 -91 -26 -91 10 0 91 97 139 166 18 26 19 34 9 48 -12 16 -20 16 -72 7 -113 -21 -114 -20 -54 55 42 53 50 70 42 83 -14 22 -32 22 -145 3z"/>
                        <path d="M997 713 c-15 -14 -5 -37 38 -90 25 -30 45 -58 45 -62 0 -4 -36 -3 -81 3 -66 7 -83 7 -90 -5 -5 -8 -7 -20 -4 -27 10 -26 140 -181 153 -182 31 -1 21 33 -31 97 -31 37 -52 69 -47 71 6 2 42 -1 81 -7 53 -9 75 -9 85 0 21 17 18 27 -24 78 -75 92 -74 84 -12 77 30 -4 75 -10 98 -13 42 -5 44 -4 40 18 -3 22 -10 25 -93 36 -107 14 -149 15 -158 6z"/>
                        <path d="M630 498 c-35 -82 -77 -205 -72 -216 11 -31 37 -2 66 76 17 45 33 82 36 82 3 0 19 -34 35 -75 27 -68 32 -75 55 -73 20 3 29 15 50 70 15 37 29 70 32 73 3 3 21 -31 40 -77 33 -79 59 -106 71 -75 3 7 -16 63 -42 123 -36 85 -52 110 -68 112 -17 3 -25 -6 -41 -50 -11 -29 -25 -66 -32 -83 l-11 -30 -36 83 c-38 87 -63 106 -83 60z"/>
                    </g>
                </svg>
                <span style="display: inline-block; vertical-align: middle; margin-left: 8px; font-size: 14px; color: #666666; font-weight: 500;">Master Education LMS</span>
            </div>
          </div>
    
          <!-- Content -->
          <div style="margin-bottom: 32px">
            <p style="margin: 0 0 16px; font-size: 15px">{greeting}</p>
            <p style="margin: 0 0 24px; font-size: 15px">
              {message}
            </p>
    
            <div
              style="
                background-color: #fff7ed;
                padding: 16px;
                border-radius: 6px;
                border: 1px solid #fed7aa;
                margin-bottom: 24px;
              "
            >
              <div style="font-size: 16px; font-weight: 600; color: #ea580c; margin-bottom: 12px;">
                ⏰ Starting in 30 minutes
              </div>
              
              <div style="margin-bottom: 8px;">
                <div style="font-size: 13px; color: #666666; margin-bottom: 4px">Lesson</div>
                <div style="font-size: 15px; font-weight: 500; color: #111111">{lesson_title}</div>
              </div>
              
              <div style="margin-bottom: 8px;">
                <div style="font-size: 13px; color: #666666; margin-bottom: 4px">Group</div>
                <div style="font-size: 15px; font-weight: 500; color: #111111">{group_name}</div>
              </div>
              
              <div>
                <div style="font-size: 13px; color: #666666; margin-bottom: 4px">Time</div>
                <div style="font-size: 15px; font-weight: 500; color: #111111">{lesson_datetime}</div>
              </div>
            </div>
    
            <p style="margin: 0; font-size: 15px; color: #666666;">
              {action_text}
            </p>
          </div>
    
          <!-- Action -->
          <div style="margin-bottom: 40px">
            <a
              href="{_build_lms_url()}"
              style="
                display: inline-block;
                background-color: #2563eb;
                color: #ffffff;
                padding: 10px 20px;
                text-decoration: none;
                border-radius: 4px;
                font-size: 14px;
                font-weight: 500;
              "
              >Go to LMS</a
            >
          </div>
    
          <!-- Footer -->
          <div style="border-top: 1px solid #e5e7eb; padding-top: 20px">
            <p style="margin: 0; font-size: 12px; color: #999999">
              Master Education<br />
              You are receiving this email because you are enrolled in {group_name}.
            </p>
          </div>
        </div>
      </body>
    </html>
    """
    
    text_content = f"""
    Lesson Reminder
    
    {greeting}
    
    {message}
    
    Lesson: {lesson_title}
    Group: {group_name}
    Time: {lesson_datetime}
    
    {action_text}
    
    Best regards,
    Master Education Team
    """
    
    logger.info(f"📤 [REMINDER] Sending email to {to_email}...")
    result = service.send_email(
        [to_email], subject, html_content, text_content,
        event_type="lesson_reminder",
        recipient_user_id=user_id,
        related_type="event",
        related_id=event_id,
        idempotency_key=reminder_idempotency_key(event_id, user_id),
    )

    if result:
        logger.info(f"✅ [REMINDER] Successfully sent reminder to {to_email}")
    else:
        logger.error(f"❌ [REMINDER] Failed to send reminder to {to_email}")
    
    return result


def send_lesson_change_curator_notification(
    curator_email: str,
    curator_name: str,
    group_name: str,
    request_type: str,
    original_datetime: str,
    *,
    new_datetime: Optional[str] = None,
    substitute_name: Optional[str] = None,
    requester_name: Optional[str] = None,
    reason: Optional[str] = None,
    curator_id: Optional[int] = None,
    lesson_request_id: Optional[int] = None,
) -> Optional[dict]:
    """Notify group curator by email when a lesson change request is approved."""
    service = get_email_service()

    type_labels = {
        "cancel": "отмена урока",
        "reschedule": "перенос урока",
        "substitution": "замена учителя",
    }
    type_label = type_labels.get(request_type, request_type)

    subject = f"Изменение расписания: {group_name} — {type_label}"

    details_rows = [
        f"<tr><td style='padding:8px 0;color:#666;font-size:13px;'>Группа</td>"
        f"<td style='padding:8px 0;font-weight:500;'>{group_name}</td></tr>",
        f"<tr><td style='padding:8px 0;color:#666;font-size:13px;'>Тип</td>"
        f"<td style='padding:8px 0;font-weight:500;'>{type_label}</td></tr>",
        f"<tr><td style='padding:8px 0;color:#666;font-size:13px;'>Дата урока</td>"
        f"<td style='padding:8px 0;font-weight:500;'>{original_datetime}</td></tr>",
    ]
    if requester_name:
        details_rows.append(
            f"<tr><td style='padding:8px 0;color:#666;font-size:13px;'>Учитель</td>"
            f"<td style='padding:8px 0;font-weight:500;'>{requester_name}</td></tr>"
        )
    if request_type == "reschedule" and new_datetime:
        details_rows.append(
            f"<tr><td style='padding:8px 0;color:#666;font-size:13px;'>Новая дата</td>"
            f"<td style='padding:8px 0;font-weight:500;'>{new_datetime}</td></tr>"
        )
    if request_type == "substitution" and substitute_name:
        details_rows.append(
            f"<tr><td style='padding:8px 0;color:#666;font-size:13px;'>Замена</td>"
            f"<td style='padding:8px 0;font-weight:500;'>{substitute_name}</td></tr>"
        )
    if reason:
        details_rows.append(
            f"<tr><td style='padding:8px 0;color:#666;font-size:13px;vertical-align:top;'>Причина</td>"
            f"<td style='padding:8px 0;'>{reason}</td></tr>"
        )

    details_html = "".join(details_rows)

    html_content = f"""
    <!DOCTYPE html>
    <html>
      <head><meta charset="utf-8" /></head>
      <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#333;line-height:1.5;margin:0;padding:0;">
        <div style="max-width:520px;margin:40px auto;padding:20px;">
          <h1 style="font-size:20px;font-weight:600;color:#111;margin:0 0 24px;">Изменение расписания</h1>
          <p style="margin:0 0 16px;font-size:15px;">Здравствуйте, {curator_name}!</p>
          <p style="margin:0 0 24px;font-size:15px;">
            Запрос педагога на изменение урока одобрен старшим преподавателем.
          </p>
          <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">{details_html}</table>
          <p style="margin:0;font-size:13px;color:#999;">Master Education LMS</p>
        </div>
      </body>
    </html>
    """

    text_lines = [
        f"Здравствуйте, {curator_name}!",
        "",
        f"Запрос на {type_label} для группы {group_name} был одобрен.",
        f"Дата урока: {original_datetime}",
    ]
    if requester_name:
        text_lines.append(f"Учитель: {requester_name}")
    if request_type == "reschedule" and new_datetime:
        text_lines.append(f"Новая дата: {new_datetime}")
    if request_type == "substitution" and substitute_name:
        text_lines.append(f"Замена: {substitute_name}")
    if reason:
        text_lines.append(f"Причина: {reason}")

    return service.send_email(
        [curator_email], subject, html_content, "\n".join(text_lines),
        event_type="lesson_change",
        recipient_user_id=curator_id,
        related_type="lesson_request",
        related_id=lesson_request_id,
    )
