from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class AskRequest(BaseModel):
    query: str
    session_id: Optional[str] = None


class AskResponse(BaseModel):
    answer: str
    escalated: bool
    escalation_reason: Optional[str] = None
    matched_entry_id: Optional[int] = None
    confidence: str  # "high" | "medium" | "low"


class FAQEntryIn(BaseModel):
    question_canonical: str
    question_variants: List[str] = []
    answer: str
    category: str
    sources: List[dict] = []
    volatile: bool = False
    recheck_interval_days: Optional[int] = None


class FAQEntryOut(FAQEntryIn):
    id: int
    status: str
    last_verified_at: datetime

    class Config:
        from_attributes = True


class ReviewQueueOut(BaseModel):
    id: int
    faq_entry_id: Optional[int]
    related_query_id: Optional[int] = None
    reason: str
    old_answer: Optional[str]
    proposed_answer: Optional[str]
    source_diff_notes: Optional[str]
    status: str
    flagged_at: datetime

    class Config:
        from_attributes = True
