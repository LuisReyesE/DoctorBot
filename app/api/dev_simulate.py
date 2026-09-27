from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.providers import get_messaging_client, get_mock_client
from app.providers.base import IncomingMessage
from app.providers.mock import MockMessagingClient
from app.schemas import SimulateMessageRequest, SimulateMessageResponse
from app.services.conversation import handle_incoming

router = APIRouter(prefix="/dev", tags=["dev"])


@router.post("/simulate", response_model=SimulateMessageResponse)
async def simulate_message(
    body: SimulateMessageRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SimulateMessageResponse:
    """Simula un mensaje entrante (solo development + provider mock)."""
    if not settings.is_dev:
        raise HTTPException(status_code=404, detail="Not available outside development")
    if settings.messaging_provider != "mock":
        raise HTTPException(
            status_code=400,
            detail="Simulación solo con MESSAGING_PROVIDER=mock",
        )

    client = get_messaging_client(settings)
    if not isinstance(client, MockMessagingClient):
        client = get_mock_client()

    before = len(client.sent)
    ext = body.external_id
    msg = IncomingMessage(
        external_id=ext,
        body=body.body,
        message_id="sim",
        media_url=body.media_url,
    )
    await handle_incoming(db, client, msg)
    replies = client.sent[before:]
    return SimulateMessageResponse(ok=True, replies=replies)


@router.get("/outbox")
async def outbox(
    settings: Settings = Depends(get_settings),
) -> dict:
    """Lista mensajes salientes del mock (demo)."""
    if not settings.is_dev or settings.messaging_provider != "mock":
        raise HTTPException(status_code=404, detail="Not available")
    client = get_mock_client()
    return {"count": len(client.sent), "messages": client.sent}


@router.delete("/outbox")
async def clear_outbox(
    settings: Settings = Depends(get_settings),
) -> dict:
    if not settings.is_dev or settings.messaging_provider != "mock":
        raise HTTPException(status_code=404, detail="Not available")
    client = get_mock_client()
    client.clear()
    return {"ok": True}
