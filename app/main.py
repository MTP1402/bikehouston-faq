from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import List

from app.database import get_db, engine, Base
from app import models, schemas, search, ai

APP_VERSION = "0.1.0"

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
    )


# ---- Admin routes (no auth yet — add before this is public-facing) ----

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
def resolve_review_item(item_id: int, resolved_by: str, db: Session = Depends(get_db)):
    item = db.query(models.ReviewQueueItem).filter(models.ReviewQueueItem.id == item_id).first()
    if not item:
        return {"error": "not found"}
    item.status = "resolved"
    item.resolved_by = resolved_by
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
