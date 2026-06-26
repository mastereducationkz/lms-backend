"""
Utility for sending push notifications via Expo Push Notification service.
"""
import requests
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

EXPO_PUSH_ENDPOINT = "https://exp.host/--/api/v2/push/send"


def send_push_notification(
    push_token: str,
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
    sound: str = "default",
    priority: str = "high",
    badge: Optional[int] = None
) -> bool:
    """
    Send a push notification to a single device.
    
    Args:
        push_token: Expo push token (starts with ExponentPushToken[...])
        title: Notification title
        body: Notification body/message
        data: Additional data to send with notification
        sound: Sound to play ('default' or None)
        priority: 'default', 'normal', or 'high'
        badge: Badge count to display
        
    Returns:
        bool: True if notification was sent successfully
    """
    if not push_token or not push_token.startswith('ExponentPushToken['):
        logger.warning(f"Invalid push token format: {push_token}")
        return False
    
    message = {
        "to": push_token,
        "title": title,
        "body": body,
        "sound": sound,
        "priority": priority,
    }
    
    if data:
        message["data"] = data
    
    if badge is not None:
        message["badge"] = badge
    
    try:
        response = requests.post(
            EXPO_PUSH_ENDPOINT,
            json=message,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=10
        )
        
        if response.status_code == 200:
            result = response.json()
            data_result = result.get("data", [{}])[0]
            
            if data_result.get("status") == "ok":
                logger.info(f"Push notification sent successfully to {push_token[:20]}...")
                return True
            else:
                error = data_result.get("message", "Unknown error")
                logger.error(f"Expo push error: {error}")
                return False
        else:
            logger.error(f"Failed to send push notification: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Exception sending push notification: {str(e)}")
        return False


def send_push_notifications_batch(
    messages: List[Dict[str, Any]]
) -> Dict[str, int]:
    """
    Send multiple push notifications in a single batch request.
    
    Args:
        messages: List of message dictionaries with keys: to, title, body, data, etc.
        
    Returns:
        dict: Statistics with 'success' and 'failed' counts
    """
    if not messages:
        return {"success": 0, "failed": 0}
    
    # Validate all tokens
    valid_messages = []
    for msg in messages:
        token = msg.get("to", "")
        if token and token.startswith('ExponentPushToken['):
            valid_messages.append(msg)
        else:
            logger.warning(f"Skipping invalid token: {token}")
    
    if not valid_messages:
        return {"success": 0, "failed": len(messages)}
    
    try:
        response = requests.post(
            EXPO_PUSH_ENDPOINT,
            json=valid_messages,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            data_results = result.get("data", [])
            
            success_count = sum(1 for r in data_results if r.get("status") == "ok")
            failed_count = len(data_results) - success_count
            
            logger.info(f"Batch push notifications: {success_count} success, {failed_count} failed")
            
            return {"success": success_count, "failed": failed_count}
        else:
            logger.error(f"Batch push request failed: {response.status_code}")
            return {"success": 0, "failed": len(valid_messages)}
            
    except Exception as e:
        logger.error(f"Exception in batch push: {str(e)}")
        return {"success": 0, "failed": len(valid_messages)}


def send_message_notification(
    push_token: str,
    sender_name: str,
    message_preview: str,
    partner_id: int
) -> bool:
    """
    Send a notification for a new message.
    
    Args:
        push_token: Recipient's push token
        sender_name: Name of the message sender
        message_preview: Preview of the message content
        partner_id: ID of the conversation partner (for navigation)
        
    Returns:
        bool: True if sent successfully
    """
    # Truncate preview if too long
    if len(message_preview) > 100:
        message_preview = message_preview[:97] + "..."
    
    return send_push_notification(
        push_token=push_token,
        title=f"New message from {sender_name}",
        body=message_preview,
        data={
            "type": "message",
            "partnerId": partner_id,
            "partnerName": sender_name,
        },
        badge=1
    )


def send_message_push_to_user(
    db,
    recipient_id: int,
    sender_name: str,
    message_preview: str,
    partner_id: int,
) -> int:
    """Send a new-message push to ALL of a recipient's active devices.

    Reads tokens from ``user_push_tokens`` (plus the legacy ``users.push_token``
    as a fallback), sends one batched Expo request, and deactivates any token
    that Expo reports as ``DeviceNotRegistered``. Returns the count accepted.

    Non-fatal: any failure is logged and swallowed so message delivery is never
    blocked by push problems.
    """
    from src.auth.models import UserInDB, UserPushToken

    try:
        token_rows = db.query(UserPushToken).filter(
            UserPushToken.user_id == recipient_id,
            UserPushToken.is_active == True,  # noqa: E712
        ).all()
        # Map token -> owning row (None for the legacy column, which has no row).
        tokens: Dict[str, Any] = {row.token: row for row in token_rows}

        recipient = db.query(UserInDB).filter(UserInDB.id == recipient_id).first()
        if recipient and recipient.push_token and recipient.push_token not in tokens:
            tokens[recipient.push_token] = None

        valid_tokens = [t for t in tokens if t and t.startswith("ExponentPushToken[")]
        if not valid_tokens:
            return 0

        preview = message_preview if len(message_preview) <= 100 else message_preview[:97] + "..."
        messages = [
            {
                "to": t,
                "title": f"New message from {sender_name}",
                "body": preview,
                "sound": "default",
                "priority": "high",
                "badge": 1,
                "data": {"type": "message", "partnerId": partner_id, "partnerName": sender_name},
            }
            for t in valid_tokens
        ]

        response = requests.post(
            EXPO_PUSH_ENDPOINT,
            json=messages,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )
        if response.status_code != 200:
            logger.error(f"Message push failed: {response.status_code} - {response.text[:200]}")
            return 0

        results = response.json().get("data", [])
        accepted = 0
        deactivated = False
        for token, result in zip(valid_tokens, results):
            if result.get("status") == "ok":
                accepted += 1
                continue
            details = result.get("details") or {}
            if details.get("error") == "DeviceNotRegistered":
                row = tokens.get(token)
                if row is not None:
                    row.is_active = False
                    deactivated = True
                if recipient and recipient.push_token == token:
                    recipient.push_token = None
                    deactivated = True
        if deactivated:
            db.commit()
        return accepted
    except Exception as e:  # never let push break messaging
        logger.error(f"send_message_push_to_user failed for {recipient_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return 0
