from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import List

from app.database import get_db, engine, Base
from app import models, schemas, search, ai

APP_VERSION = "0.2.0"

Base.metadata.create_all(bind=engine)

app = FastAPI(title="BikeHouston FAQ API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to bikehouston.org / your GitHub Pages domain before launch
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"status": "ok", "version": APP_VERSION}


@app.post("/ask", response_model=schemas.AskResponse)
def ask(payload: schemas.AskRequest, db: Session = Depends(get_db)):
    match, score = search.find_best_match(db, payload.query)

    answer_text = None
    escalated = False
    escalation_reason = None
    matched_entry_id = None
    confidence = "low"

    if match and score >= search.HIGH_CONFIDENCE:
        # Strong match — answer straight from the curated knowledge base
        confidence = "high"
        answer_text = match.answer
        matched_entry_id = match.id

    elif match and score >= search.MEDIUM_CONFIDENCE:
        # Partial match — let the AI answer, grounded in the closest KB entry
        confidence = "medium"
        matched_entry_id = match.id
        kb_context = f"Closest known answer (may be a related but not identical question): {match.answer}"
        result = ai.generate_answer(payload.query, kb_context)
        if not result.get("on_topic", True):
            escalated = False
            answer_text = result["answer"]
        elif not result.get("confident", False) or result.get("needs_human"):
            escalated = True
            escalation_reason = result.get("needs_human_reason") or "low_confidence"
            answer_text = ("Good question — I want to make sure you get an accurate answer, "
                            "so I'm passing this to someone at BikeHouston who can follow up.")
        else:
            answer_text = result["answer"]

    else:
        # No good match — let the AI attempt cold, but default to escalation on any doubt
        result = ai.generate_answer(payload.query)
        if not result.get("on_topic", True):
            answer_text = result["answer"]
        elif not result.get("confident", False) or result.get("needs_human"):
            escalated = True
            escalation_reason = result.get("needs_human_reason") or "no_kb_match"
            answer_text = ("That's outside what I can confidently answer right now — "
                            "I'm passing it to someone at BikeHouston who can help.")
        else:
            confidence = "medium"
            answer_text = result["answer"]

    # Log every query for the admin dashboard, regardless of outcome
    log_entry = models.UserQuery(
        query_text=payload.query,
        matched_entry_id=matched_entry_id,
        match_score=score,
        was_escalated=escalated,
        escalation_reason=escalation_reason,
        ai_answer=answer_text,
        session_id=payload.session_id,
        asker_email=payload.email,
    )
    db.add(log_entry)
    db.commit()
    db.refresh(log_entry)

    if escalated:
        review_item = models.ReviewQueueItem(
            faq_entry_id=matched_entry_id,
            reason="user_escalation",
            proposed_answer=None,
            related_query_id=log_entry.id,
            status="open",
        )
        db.add(review_item)
        db.commit()

    return schemas.AskResponse(
        answer=answer_text,
        escalated=escalated,
        escalation_reason=escalation_reason,
        matched_entry_id=matched_entry_id,
        confidence=confidence,
        query_id=log_entry.id,
    )


@app.get("/popular")
def popular_questions(limit: int = 10, db: Session = Depends(get_db)):
    """
    Public endpoint: the most-asked questions, ranked by how often each
    published KB entry has been matched. Powers the 'Most Asked' section
    on the frontend. Only surfaces already-published FAQ content — never
    raw user-typed query text — so it's safe to expose without auth.
    """
    rows = (
        db.query(models.UserQuery.matched_entry_id, func.count(models.UserQuery.id).label("ask_count"))
        .filter(models.UserQuery.matched_entry_id.isnot(None))
        .group_by(models.UserQuery.matched_entry_id)
        .order_by(desc("ask_count"))
        .limit(limit)
        .all()
    )

    entry_ids = [r[0] for r in rows]
    if not entry_ids:
        return []

    entries = {
        e.id: e
        for e in db.query(models.FAQEntry)
        .filter(models.FAQEntry.id.in_(entry_ids), models.FAQEntry.status == "published")
        .all()
    }

    result = []
    for entry_id, ask_count in rows:
        entry = entries.get(entry_id)
        if entry:
            result.append({
                "id": entry.id,
                "question": entry.question_canonical,
                "answer": entry.answer,
                "ask_count": ask_count,
            })
    return result


@app.get("/answered", response_model=List[schemas.AnsweredItem])
def list_answered(limit: int = 20, db: Session = Depends(get_db)):
    """
    Public endpoint: questions that were escalated to a human and have
    since been given a real answer. Lets anyone browse for their question
    without needing to leave an email or wait for a personal reply.
    """
    rows = (
        db.query(models.ReviewQueueItem, models.UserQuery)
        .join(models.UserQuery, models.ReviewQueueItem.related_query_id == models.UserQuery.id)
        .filter(
            models.ReviewQueueItem.status == "resolved",
            models.ReviewQueueItem.proposed_answer.isnot(None),
        )
        .order_by(desc(models.ReviewQueueItem.resolved_at))
        .limit(limit)
        .all()
    )
    return [
        schemas.AnsweredItem(
            question=query.query_text,
            answer=item.proposed_answer,
            resolved_at=item.resolved_at,
        )
        for item, query in rows
    ]


@app.get("/browse")
def browse_faq(search: str = "", limit: int = 200, db: Session = Depends(get_db)):
    """
    Public endpoint: every published FAQ entry, ranked by how often it's
    been asked (most first), with an optional search filter. Powers the
    'browse previous questions' page.
    """
    ask_counts = dict(
        db.query(models.UserQuery.matched_entry_id, func.count(models.UserQuery.id))
        .filter(models.UserQuery.matched_entry_id.isnot(None))
        .group_by(models.UserQuery.matched_entry_id)
        .all()
    )

    query = db.query(models.FAQEntry).filter(models.FAQEntry.status == "published")
    if search:
        like = f"%{search}%"
        query = query.filter(models.FAQEntry.question_canonical.ilike(like))

    entries = query.all()
    result = [
        {
            "id": e.id,
            "question": e.question_canonical,
            "answer": e.answer,
            "category": e.category,
            "ask_count": ask_counts.get(e.id, 0),
            "helpful_count": e.helpful_count or 0,
            "unhelpful_count": e.unhelpful_count or 0,
        }
        for e in entries
    ]
    result.sort(key=lambda r: r["ask_count"], reverse=True)
    return result[:limit]


@app.post("/faq/{entry_id}/feedback")
def faq_feedback(entry_id: int, helpful: bool, db: Session = Depends(get_db)):
    """Public endpoint: thumbs up/down on a published FAQ answer."""
    entry = db.query(models.FAQEntry).filter(models.FAQEntry.id == entry_id).first()
    if not entry:
        return {"error": "not found"}
    if helpful:
        entry.helpful_count = (entry.helpful_count or 0) + 1
    else:
        entry.unhelpful_count = (entry.unhelpful_count or 0) + 1
    db.commit()
    return {"helpful_count": entry.helpful_count, "unhelpful_count": entry.unhelpful_count}


# ---- Admin routes (no auth yet — add before this is public-facing) ----

@app.get("/admin/stats")
def admin_stats(db: Session = Depends(get_db)):
    """
    Summary report for an admin: total volume, how confident the matches
    were, how often we escalated to a human, and what's still waiting for
    a human answer. Confidence is bucketed from the trigram match_score
    saved on each query (search.HIGH_CONFIDENCE / MEDIUM_CONFIDENCE), since
    that's the same threshold /ask itself uses to decide whether to answer
    directly or hand off.
    """
    total = db.query(func.count(models.UserQuery.id)).scalar() or 0
    escalated = (
        db.query(func.count(models.UserQuery.id))
        .filter(models.UserQuery.was_escalated.is_(True))
        .scalar() or 0
    )

    high = (
        db.query(func.count(models.UserQuery.id))
        .filter(models.UserQuery.match_score >= search.HIGH_CONFIDENCE)
        .scalar() or 0
    )
    medium = (
        db.query(func.count(models.UserQuery.id))
        .filter(
            models.UserQuery.match_score >= search.MEDIUM_CONFIDENCE,
            models.UserQuery.match_score < search.HIGH_CONFIDENCE,
        )
        .scalar() or 0
    )
    low = total - high - medium

    reason_rows = (
        db.query(models.UserQuery.escalation_reason, func.count(models.UserQuery.id))
        .filter(models.UserQuery.was_escalated.is_(True))
        .group_by(models.UserQuery.escalation_reason)
        .order_by(desc(func.count(models.UserQuery.id)))
        .all()
    )
    escalation_reasons = [
        {"reason": reason or "unspecified", "count": count} for reason, count in reason_rows
    ]

    open_review_count = (
        db.query(func.count(models.ReviewQueueItem.id))
        .filter(models.ReviewQueueItem.status == "open")
        .scalar() or 0
    )

    return {
        "total_questions": total,
        "escalated_count": escalated,
        "escalated_pct": round(100 * escalated / total, 1) if total else 0,
        "confidence_breakdown": {
            "high": high,
            "medium": medium,
            "low": low,
        },
        "top_escalation_reasons": escalation_reasons,
        "open_review_count": open_review_count,
    }


@app.get("/admin/queries")
def list_queries(limit: int = 50, db: Session = Depends(get_db)):
    rows = (
        db.query(models.UserQuery)
        .order_by(desc(models.UserQuery.created_at))
        .limit(limit)
        .all()
    )
    return rows


@app.get("/admin/review-queue", response_model=List[schemas.ReviewQueueOut])
def list_review_queue(status: str = "open", db: Session = Depends(get_db)):
    rows = (
        db.query(models.ReviewQueueItem)
        .filter(models.ReviewQueueItem.status == status)
        .order_by(desc(models.ReviewQueueItem.flagged_at))
        .all()
    )
    return rows


@app.post("/admin/review-queue/{item_id}/resolve")
def resolve_review_item(item_id: int, payload: schemas.ResolveRequest, db: Session = Depends(get_db)):
    item = db.query(models.ReviewQueueItem).filter(models.ReviewQueueItem.id == item_id).first()
    if not item:
        return {"error": "not found"}
    item.status = "resolved"
    item.resolved_by = payload.resolved_by
    item.resolved_at = func.now()
    if payload.answer:
        item.proposed_answer = payload.answer
        # also update the original logged query so the admin dashboard
        # shows the real answer instead of the "passing to a human" placeholder
        if item.related_query_id:
            related = db.query(models.UserQuery).filter(models.UserQuery.id == item.related_query_id).first()
            if related:
                related.ai_answer = payload.answer
    db.commit()
    return {"ok": True}


@app.get("/admin/faq", response_model=List[schemas.FAQEntryOut])
def list_faq(db: Session = Depends(get_db)):
    return db.query(models.FAQEntry).order_by(models.FAQEntry.category).all()


@app.post("/admin/faq", response_model=schemas.FAQEntryOut)
def create_faq(entry: schemas.FAQEntryIn, db: Session = Depends(get_db)):
    db_entry = models.FAQEntry(**entry.dict())
    db.add(db_entry)
    db.commit()
    db.refresh(db_entry)
    return db_entry


@app.put("/admin/faq/{entry_id}", response_model=schemas.FAQEntryOut)
def update_faq(entry_id: int, entry: schemas.FAQEntryIn, db: Session = Depends(get_db)):
    db_entry = db.query(models.FAQEntry).filter(models.FAQEntry.id == entry_id).first()
    if not db_entry:
        return {"error": "not found"}
    for k, v in entry.dict().items():
        setattr(db_entry, k, v)
    db.commit()
    db.refresh(db_entry)
    return db_entry
