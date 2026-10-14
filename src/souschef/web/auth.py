"""The one account every request acts as.

The lowest-id active user owns the kitchen. The first request creates that
account when the database has none.
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.db import get_db
from souschef.models import User

# The column is NOT NULL. Nothing checks the value.
_UNCHECKED_HASH = "!"


def sole_user(db: Session) -> User:
    """Lowest-id active account, created on first use."""
    user = db.scalar(select(User).where(User.is_active.is_(True)).order_by(User.id))
    if user is None:
        user = User(
            username="owner",
            display_name="owner",
            password_hash=_UNCHECKED_HASH,
            is_admin=True,
        )
        db.add(user)
        db.flush()
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency: every request acts as the sole account."""
    user = sole_user(db)
    request.state.user = user
    return user
