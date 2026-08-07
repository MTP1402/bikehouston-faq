from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey,
    ARRAY, Float, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base


class FAQEntry(Base):
    __tablename__ = "faq_entries"

    id = Column(Integer, primary_key=True, index=True)
    question_canonical = Column(Text, nullable=False)
    question_variants = Column(ARRAY(Text), default=list)  # alternate phrasings for matching
    answer = Column(Text, nullable=False)
    category = Column(String(64), index=True)  # e.g. "legal", "infrastructure", "programs"

    sources = Column(JSON, default=list)  # [{"url": ..., "note": ...}]

    volatile = Column(Boolean, default=False)  # does this need periodic re-verification?
    recheck_interval_days = Column(Integer, nullable=True)  # e.g. 90, 365
    last_verified_at = Column(DateTime(timezone=True), server_default=func.now())

    status = Column(String(32), default="published")  # draft | published | needs_review

    helpful_count = Column(Integer, default=0)
    unhelpful_count = Column(Integer, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserQuery(Base):
    __tablename__ = "user_queries"

    id = Column(Integer, primary_key=True, index=True)
    query_text = Column(Text, nullable=False)
    matched_entry_id = Column(Integer, ForeignKey("faq_entries.id"), nullable=True)
    match_score = Column(Float, nullable=True)  # similarity score of best match, 0-1

    was_escalated = Column(Boolean, default=False)
    escalation_reason = Column(String(128), nullable=True)

    ai_answer = Column(Text, nullable=True)  # what was actually shown to the user

    session_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    matched_entry = relationship("FAQEntry")


    asker_email = Column(String(255), nullable=True)  # optional, for a personal follow-up

class ReviewQueueItem(Base):
    __tablename__ = "review_queue"

    id = Column(Integer, primary_key=True, index=True)
    faq_entry_id = Column(Integer, ForeignKey("faq_entries.id"), nullable=True)

    reason = Column(String(64))  # "stale_check" | "user_escalation" | "low_confidence_pattern"
    old_answer = Column(Text, nullable=True)
    proposed_answer = Column(Text, nullable=True)
    source_diff_notes = Column(Text, nullable=True)

    related_query_id = Column(Integer, ForeignKey("user_queries.id"), nullable=True)

    status = Column(String(32), default="open")  # open | resolved | dismissed
    resolved_by = Column(String(128), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    # Set when a human has actually emailed the answer back to the asker.
    # NOTE: requires a manual ALTER TABLE against the live Railway Postgres —
    # create_all() will not add this column to the existing table.
    reply_sent_at = Column(DateTime(timezone=True), nullable=True)

    flagged_at = Column(DateTime(timezone=True), server_default=func.now())
