from __future__ import annotations
from pydantic import BaseModel, HttpUrl, Field
from datetime import datetime
from typing import Optional, Dict, Any

class Job(BaseModel):
    job_id: str = Field(..., description="Stable hash: company|canonical_apply_url")
    title: str
    company: str
    location: str
    remote_type: str
    posted_date: datetime
    apply_url: HttpUrl
    ats_type: str
    canonical_apply_url: HttpUrl
    eligible: bool = False
    status: str = "new"  # new | queued | parked | applied | dropped
    meta: Dict[str, Any] = {}
