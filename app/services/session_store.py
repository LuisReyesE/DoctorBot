from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models import SessionState


def get_or_create_session(db: Session, external_id: str, role: str = "patient") -> SessionState:
    ext = str(external_id).lstrip("+")
    sess = db.query(SessionState).filter(SessionState.external_id == ext).first()
    if sess is None:
        sess = SessionState(external_id=ext, role=role, state="idle", context_json="{}")
        db.add(sess)
        db.commit()
        db.refresh(sess)
    return sess


def get_context(sess: SessionState) -> dict[str, Any]:
    try:
        return json.loads(sess.context_json or "{}")
    except json.JSONDecodeError:
        return {}


def set_context(db: Session, sess: SessionState, context: dict[str, Any]) -> None:
    sess.context_json = json.dumps(context, ensure_ascii=False)
    sess.updated_at = datetime.now(timezone.utc)
    db.add(sess)
    db.commit()


def set_state(db: Session, sess: SessionState, state: str, context: dict[str, Any] | None = None) -> None:
    sess.state = state
    if context is not None:
        sess.context_json = json.dumps(context, ensure_ascii=False)
    sess.updated_at = datetime.now(timezone.utc)
    db.add(sess)
    db.commit()
