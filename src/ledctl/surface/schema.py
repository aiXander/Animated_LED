"""Pydantic models for the `write_effect` tool call + Param schema.

The schema is the contract between the LLM, the persistence layer, and the
operator UI's dynamic param panel.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .palettes import named_palette_names

ParamControl = Literal[
    "slider", "int_slider", "color", "select", "toggle", "palette"
]

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_HEX_PATTERN = re.compile(r"^#?[0-9a-fA-F]{6}$|^#?[0-9a-fA-F]{3}$")


def _check_key(key: str) -> str:
    if not _KEY_PATTERN.match(key):
        raise ValueError(
            f"param key {key!r} must be snake_case [a-z][a-z0-9_]{{0,40}}"
        )
    return key


class ParamCommon(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str | None = None
    help: str | None = None

    @model_validator(mode="after")
    def _validate_key(self) -> ParamCommon:
        _check_key(self.key)
        return self


class SliderParam(ParamCommon):
    control: Literal["slider"]
    min: float
    max: float
    step: float | None = None
    default: float
    unit: str | None = None

    @model_validator(mode="after")
    def _validate_range(self) -> SliderParam:
        if self.min > self.max:
            raise ValueError(
                f"slider {self.key!r}: min ({self.min}) > max ({self.max})"
            )
        if not (self.min <= self.default <= self.max):
            raise ValueError(
                f"slider {self.key!r}: default {self.default} is outside "
                f"[{self.min}, {self.max}]"
            )
        return self


class IntSliderParam(ParamCommon):
    control: Literal["int_slider"]
    min: int
    max: int
    step: int | None = 1
    default: int

    @model_validator(mode="after")
    def _validate_range(self) -> IntSliderParam:
        if self.min > self.max:
            raise ValueError(
                f"int_slider {self.key!r}: min ({self.min}) > max ({self.max})"
            )
        if not (self.min <= self.default <= self.max):
            raise ValueError(
                f"int_slider {self.key!r}: default {self.default} is outside "
                f"[{self.min}, {self.max}]"
            )
        return self


class ColorParam(ParamCommon):
    control: Literal["color"]
    default: str  # hex

    @model_validator(mode="after")
    def _validate_hex(self) -> ColorParam:
        if not _HEX_PATTERN.match(self.default):
            raise ValueError(
                f"color {self.key!r}: default {self.default!r} is not a valid "
                f"#rrggbb / #rgb hex string"
            )
        return self


class SelectParam(ParamCommon):
    control: Literal["select"]
    options: list[str]
    default: str

    @model_validator(mode="after")
    def _validate_default_in_options(self) -> SelectParam:
        if not self.options:
            raise ValueError(f"select {self.key!r}: options must be non-empty")
        if self.default not in self.options:
            raise ValueError(
                f"select {self.key!r}: default {self.default!r} is not in "
                f"options {self.options}"
            )
        return self


class ToggleParam(ParamCommon):
    control: Literal["toggle"]
    default: bool


class PaletteParam(ParamCommon):
    control: Literal["palette"]
    default: str

    @model_validator(mode="after")
    def _validate_palette_name(self) -> PaletteParam:
        known = named_palette_names()
        if self.default not in known:
            raise ValueError(
                f"palette {self.key!r}: default {self.default!r} is not a "
                f"known palette. Valid names: {known}"
            )
        return self


ParamSpec = Annotated[
    SliderParam | IntSliderParam | ColorParam | SelectParam | ToggleParam | PaletteParam,
    Field(discriminator="control"),
]


class WriteEffectArgs(BaseModel):
    """Arguments to the single `write_effect` tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,40}$")
    summary: str = Field("", max_length=400)
    code: str = Field(..., min_length=1, max_length=8 * 1024)
    params: list[ParamSpec] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _no_duplicate_keys(self) -> WriteEffectArgs:
        seen: set[str] = set()
        for p in self.params:
            if p.key in seen:
                raise ValueError(f"duplicate param key: {p.key}")
            seen.add(p.key)
        return self


def param_to_dict(p: ParamSpec) -> dict[str, Any]:
    return p.model_dump()
