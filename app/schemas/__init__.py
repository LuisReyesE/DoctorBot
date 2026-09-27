from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class SimulateMessageRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    external_id: str = Field(
        ...,
        validation_alias=AliasChoices("external_id", "from_phone"),
        description="ID del usuario en el canal (teléfono WA o chat_id Telegram)",
    )
    body: str = Field(..., description="Texto del mensaje")
    media_url: str | None = Field(
        default=None,
        description="Ruta local o file_id para simular una foto",
    )


class SimulateMessageResponse(BaseModel):
    ok: bool = True
    replies: list[dict]


class HealthResponse(BaseModel):
    status: str
    version: str
    provider: str
