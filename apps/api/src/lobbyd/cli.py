"""Administrative commands: `lobbyd-admin set-role <email> <role>`."""

import argparse
import asyncio
import sys

from sqlalchemy import update

from lobbyd.config import get_settings
from lobbyd.identity.models import ROLES, User
from lobbyd.platform.db import create_engine, create_sessionmaker


async def _set_role(email: str, role: str) -> bool:
    engine = create_engine(get_settings())
    try:
        async with create_sessionmaker(engine).begin() as session:
            result = await session.execute(
                update(User).where(User.email == email).values(role=role).returning(User.id)
            )
            return result.one_or_none() is not None
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="lobbyd-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    set_role = commands.add_parser("set-role", help="Change an account's role")
    set_role.add_argument("email")
    set_role.add_argument("role", choices=ROLES)
    args = parser.parse_args()

    if asyncio.run(_set_role(args.email, args.role)):
        sys.stdout.write(f"{args.email} is now {args.role}\n")
    else:
        sys.exit(f"No account with email {args.email}")


if __name__ == "__main__":
    main()
