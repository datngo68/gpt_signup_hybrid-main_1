from pydantic import BaseModel, Field


class AutoRegStartRequest(BaseModel):
    """Body cho POST /api/icloud/autoreg/start."""

    # Default 3 — phù hợp browser pool (1 Camoufox xuyên 3 signup parallel).
    # Bump khoảng tới 5 nếu RAM cho phép (mỗi context ~50-100MB).
    concurrency: int = Field(default=3, ge=1, le=5)
    poll_interval: int = Field(default=30, ge=10, description="Seconds between poll cycles")
    default_password: str = Field(default="", description="Falls back to reg.default_password from Settings if empty")
    logs_url: str = Field(default="", description="Worker API URL (fallback to env HYBRID_WORKER_LOGS_URL)")
    api_key: str = Field(default="", description="Worker API key (fallback to env HYBRID_WORKER_API_KEY)")


class AutoRegStatusResponse(BaseModel):
    """Response cho GET /api/icloud/autoreg/status."""

    running: bool
    processed: int
    success: int
    errors: int
    current_cycle: int


class ChatGptAccountRow(BaseModel):
    """Một row trong chatgpt_accounts."""

    id: int
    email: str
    password: str
    secret_2fa: str | None
    created_at: str
