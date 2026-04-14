"""Pydantic models for therapist profiles and collection results."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class TherapistProfile(BaseModel):
    """A single therapist scraped from therapie.de."""

    name: str
    website: str | None = None
    email: str | None = None
    therapist_type: str = ""
    profile_url: str = ""


class CollectionResult(BaseModel):
    """Result of a single collection run."""

    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_profiles_scraped: int = 0
    total_matching: int = 0
    therapists: list[TherapistProfile] = Field(default_factory=list)
    new_therapists: list[TherapistProfile] = Field(default_factory=list)
    contacted: list[TherapistProfile] = Field(default_factory=list)
