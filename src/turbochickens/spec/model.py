"""Pydantic v2 data model for TurboChickens TOML job specs.

Sections: [job], [trigger], [harness], [resources], [input], [output].
There is deliberately no ``max_tokens`` knob anywhere — token usage is
measured telemetry, not a declared budget. The only cost control is the
optional USD cap ``resources.max_cost_usd`` (spec 24), enforced at finalize.
Any extra key (including ``max_tokens``) is rejected with a field-named error.
"""

from __future__ import annotations

from typing import Literal

from croniter import croniter
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

ADAPTERS = ("mistral-api", "vibe-cli")
TRIGGER_TYPES = ("manual", "cron")
NETWORK_POLICIES = ("none",)  # MVP: declared policy only, enforced post-MVP
OUTPUT_FORMATS = ("json",)


class _StrictModel(BaseModel):
    """Base model that forbids unknown keys so stray fields are named errors."""

    model_config = ConfigDict(extra="forbid")


def _is_valid_cron(expr: str) -> bool:
    """Standard 5-field cron expression check."""
    if len(expr.split()) != 5:
        return False
    try:
        croniter(expr)
    except (ValueError, KeyError):
        return False
    return True


class JobSection(_StrictModel):
    name: str = Field(min_length=1)
    description: str = ""


class TriggerSection(_StrictModel):
    type: Literal["manual", "cron"]
    schedule: str | None = Field(default=None, validate_default=True)

    @field_validator("schedule")
    @classmethod
    def _check_schedule(cls, v: str | None, info: ValidationInfo) -> str | None:
        trigger_type = info.data.get("type")
        if trigger_type == "cron":
            if not v:
                raise ValueError("is required when trigger.type is 'cron'")
            if not _is_valid_cron(v):
                raise ValueError(f"{v!r} is not a valid 5-field cron expression")
        elif trigger_type == "manual" and v is not None:
            raise ValueError("must not be set when trigger.type is 'manual'")
        return v


class HarnessSection(_StrictModel):
    adapter: Literal["mistral-api", "vibe-cli"]
    model: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)


class ResourcesSection(_StrictModel):
    timeout_sec: int = Field(gt=0, description="hard wall-clock kill budget")
    max_context_mb: int = Field(gt=0, description="hard cap on injected context size")
    # spec 24: optional Feed Budget cost cap in USD (never a token count),
    # enforced finalize-only. Absent = no cap (off by default).
    max_cost_usd: float | None = Field(default=None, gt=0.0, description="optional USD cost cap, enforced at finalize")
    network: Literal["none"] = "none"


class InputSection(_StrictModel):
    prompt_file: str = Field(min_length=1)
    attachments: list[str] = Field(default_factory=list)


class OutputSection(_StrictModel):
    format: Literal["json"]
    schema_file: str = Field(min_length=1)


class JobSpec(_StrictModel):
    job: JobSection
    trigger: TriggerSection
    harness: HarnessSection
    resources: ResourcesSection
    input: InputSection
    output: OutputSection
