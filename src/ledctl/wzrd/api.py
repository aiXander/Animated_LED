"""HTTP routes for the WZRD input: `GET /wzrd`, `PATCH /wzrd`."""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, HTTPException
from pydantic import ValidationError

from .stage import WzrdStage


def install_wzrd_routes(app: FastAPI, stage: WzrdStage) -> None:
    @app.get("/wzrd")
    async def get_wzrd() -> dict:
        return stage.payload()

    @app.patch("/wzrd")
    async def patch_wzrd(body: dict[str, Any] = Body(...)) -> dict:  # noqa: B008
        """Any subset of the settings (see `WzrdSettings`); applied and persisted."""
        try:
            stage.update_settings(body)
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail=e.errors(include_url=False, include_context=False),
            ) from e
        return stage.payload()
