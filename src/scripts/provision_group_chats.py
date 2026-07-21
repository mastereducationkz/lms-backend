"""Idempotently provision class + parents chat channels for every active group.

Usage: venv/bin/python -m src.scripts.provision_group_chats
"""
from src.config import SessionLocal
from src.messages.group_membership import provision_all_groups


def main():
    db = SessionLocal()
    try:
        n = provision_all_groups(db)
        print(f"Provisioned/synced group chats for {n} active groups")
    finally:
        db.close()


if __name__ == "__main__":
    main()
