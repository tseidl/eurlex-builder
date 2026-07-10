"""Pydantic configuration models for eurlex-builder."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eurlex_builder.utils import is_valid_celex


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Metadata(StrictModel):
    project_name: str = "eurlex-builder Dataset"
    author: str = ""
    description: str = "A dataset built with eurlex-builder."
    date_created: date = Field(default_factory=date.today)
    version: str = "1.0"


class FixedMode(StrictModel):
    mode: Literal["fixed"] = "fixed"
    celex_ids: list[str] = Field(default_factory=list)
    procedure_numbers: list[str] = Field(default_factory=list)

    @field_validator("celex_ids", mode="before")
    @classmethod
    def valid_celex_ids(cls, values):
        normalized = [str(value).strip().upper() for value in (values or [])]
        invalid = [value for value in normalized if not is_valid_celex(value)]
        if invalid:
            raise ValueError(f"Invalid CELEX ID(s): {', '.join(invalid)}")
        return normalized

    @model_validator(mode="after")
    def at_least_one_input(self):
        if not self.celex_ids and not self.procedure_numbers:
            raise ValueError("Fixed mode requires at least one celex_id or procedure_number.")
        return self


class DescriptiveMode(StrictModel):
    mode: Literal["descriptive"] = "descriptive"
    # No hardcoded Literal — any document type string is accepted
    document_types: list[str]
    start_date: date
    end_date: date
    filter_keywords: list[str] = Field(default_factory=list)
    include_corrigenda: bool = False
    include_consolidated_texts: bool = False

    @field_validator("document_types")
    @classmethod
    def at_least_one_type(cls, v):
        if not v:
            raise ValueError("At least one document type is required.")
        return v

    @model_validator(mode="after")
    def dates_valid(self):
        if self.start_date >= self.end_date:
            raise ValueError("start_date must be before end_date.")
        return self


DataConfig = Annotated[Union[FixedMode, DescriptiveMode], Field(discriminator="mode")]


class TextExtraction(StrictModel):
    include_recitals: bool = True
    include_articles: bool = True
    include_annexes: bool = True
    strip_boilerplate: bool = True
    store_raw_html: bool = False
    article_granularity: Literal["article", "paragraph", "point"] = "article"


class Translation(StrictModel):
    translate_full_text: bool = True
    translate_text_units: bool = True
    max_full_text_chars: int = Field(
        default=100_000,
        ge=0,
        description="Skip full_text translation for documents longer than this. "
        "Set to 0 to disable the limit.",
    )


class Processing(StrictModel):
    automated_mode: bool = False
    text_extraction: TextExtraction = Field(default_factory=TextExtraction)
    translation: Translation = Field(default_factory=Translation)
    include_relations: bool = True
    include_eurovoc: bool = False
    fetch_original_recitals_for_consolidated: bool = True
    fetch_original_relations_for_consolidated: bool = True
    parallel: bool = False
    max_workers: int = Field(default=4, ge=1, le=16)


def _default_formats() -> list[Literal["parquet", "csv"]]:
    return ["parquet"]


class Output(StrictModel):
    formats: list[Literal["parquet", "csv"]] = Field(
        default_factory=_default_formats, min_length=1,
    )
    output_directory: str = "./output"


class Config(StrictModel):
    metadata: Metadata = Field(default_factory=Metadata)
    data: DataConfig
    processing: Processing = Field(default_factory=Processing)
    output: Output = Field(default_factory=Output)


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML configuration file."""
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)
