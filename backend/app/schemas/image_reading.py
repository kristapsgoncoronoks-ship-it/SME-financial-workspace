"""Stored facts about preparing one original image for a local reading attempt."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ImageReadingPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    preprocess_version: Literal["local-image-ocr-v1"] = "local-image-ocr-v1"
    source_width: int = Field(ge=1, le=2**31 - 1)
    source_height: int = Field(ge=1, le=2**31 - 1)
    raster_width: int | None = Field(default=None, ge=1, le=20_000)
    raster_height: int | None = Field(default=None, ge=1, le=20_000)
    resized: bool = False

    @model_validator(mode="after")
    def consistent_geometry(self) -> Self:
        if (self.raster_width is None) != (self.raster_height is None):
            raise ValueError("Reading raster dimensions must be recorded together")
        if self.raster_width is None:
            if self.resized:
                raise ValueError("A prepared raster must have measured dimensions")
        elif self.raster_height is not None:
            if self.raster_width > self.source_width or self.raster_height > self.source_height:
                raise ValueError("Reading preparation cannot enlarge the source")
            changed = (self.raster_width, self.raster_height) != (
                self.source_width,
                self.source_height,
            )
            if changed != self.resized:
                raise ValueError("Reading preparation must state the effective size change")
        return self


def load_preparation(value: str | None) -> ImageReadingPreparation | None:
    return ImageReadingPreparation.model_validate_json(value) if value is not None else None


def preparation_warning(preparation: ImageReadingPreparation | None) -> str | None:
    if preparation is None or not preparation.resized:
        return None
    return (
        f"The {preparation.source_width} × {preparation.source_height} image was reduced to "
        f"{preparation.raster_width} × {preparation.raster_height} for reading. "
        "Check the suggested fields against the original before applying them."
    )
