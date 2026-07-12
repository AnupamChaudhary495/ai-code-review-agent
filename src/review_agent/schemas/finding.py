"""Structured review findings — the contract between the LLM and the rest of the system."""

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["bug", "security", "performance", "quality"]
Severity = Literal["critical", "high", "medium", "low"]


class Finding(BaseModel):
    """One reviewable issue in one file."""

    file: str = Field(description="Path of the reviewed file, exactly as given")
    line: int | None = Field(
        default=None,
        description="Line number in the NEW version of the file; null for file-level findings",
    )
    category: Category
    severity: Severity
    message: str = Field(description="What is wrong and why it matters")
    suggestion: str | None = Field(
        default=None, description="Concrete fix, as code or a short instruction"
    )


class ReviewFindings(BaseModel):
    """Top-level object the model must emit. An empty list means a clean review."""

    findings: list[Finding]
