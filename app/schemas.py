from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class IncidentIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(..., min_length=1, max_length=300)
    description: str = Field(..., min_length=1, max_length=10_000)
    service: Optional[str] = Field(default=None, max_length=100)
    source: Optional[str] = Field(default=None, max_length=100)
    timestamp: Optional[datetime] = None


class TriageOut(BaseModel):
    incident_id: str
    predicted_severity: str = Field(pattern="^(low|medium|high|critical)$")
    duplicate_of: Optional[str] = None
    runbook_suggestion: str
    status: str
