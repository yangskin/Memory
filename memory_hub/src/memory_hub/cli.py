"""Administrative CLI placeholder; token commands are added with persistence."""

from __future__ import annotations

import argparse

from memory_hub.auth.tokens import create_token
from memory_hub.config import load_settings
from memory_hub.db.models import AccessToken
from memory_hub.db.session import create_session_factory


def main() -> int:
    parser = argparse.ArgumentParser(prog="memory-hub")
    parser.add_argument("--version", action="store_true")
    subparsers = parser.add_subparsers(dest="command")
    token_parser = subparsers.add_parser("token")
    token_subparsers = token_parser.add_subparsers(dest="token_command")
    create_parser = token_subparsers.add_parser("create")
    create_parser.add_argument("--project", required=True)
    create_parser.add_argument("--user", required=True)
    create_parser.add_argument("--scope", action="append", required=True)
    revoke_parser = token_subparsers.add_parser("revoke")
    revoke_parser.add_argument("--token-id", required=True)
    list_parser = token_subparsers.add_parser("list")
    list_parser.add_argument("--project", required=True)
    args = parser.parse_args()
    if args.version:
        print("memory-hub 0.1.0")
    elif args.command == "token" and args.token_command == "create":
        settings = load_settings()
        if not settings.database_url:
            parser.error("MEMORY_HUB_DATABASE_URL is required for token storage")
        token_id, token, secret_hash = create_token()
        with create_session_factory(settings.database_url)() as session:
            session.add(AccessToken(token_id=token_id, token_secret_hash=secret_hash, token_prefix=token[:20], user_id=args.user, project_id=args.project, scopes=args.scope))
            session.commit()
        print(token)
    elif args.command == "token" and args.token_command == "revoke":
        settings = load_settings()
        if not settings.database_url:
            parser.error("MEMORY_HUB_DATABASE_URL is required for token storage")
        with create_session_factory(settings.database_url)() as session:
            item = session.get(AccessToken, args.token_id)
            if item is None:
                parser.error("unknown token id")
            item.revoked_at = __import__("datetime").datetime.now(__import__("datetime").UTC)
            session.commit()
    elif args.command == "token" and args.token_command == "list":
        settings = load_settings()
        if not settings.database_url:
            parser.error("MEMORY_HUB_DATABASE_URL is required for token storage")
        with create_session_factory(settings.database_url)() as session:
            for item in session.query(AccessToken).filter_by(project_id=args.project).order_by(AccessToken.created_at):
                print(f"{item.token_id}\t{item.user_id}\t{','.join(item.scopes)}\t{'revoked' if item.revoked_at else 'active'}")
    else:
        parser.print_help()
    return 0