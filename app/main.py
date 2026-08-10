import os
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import List, Optional

from app.database import get_db, engine, Base
from app import models, schemas, search, ai

APP_VERSION = "0.3.0"

# ---- Admin auth (shared password) ----
# Set ADMIN_PASSWORD in the Railway service variables. This is a single shared
# password, NOT per-user accounts — it exists to keep the admin routes from
# being wide open. Real per-user auth should hook into BikeHouston's own
# identity system and is deliberately left for whoever takes this over.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def require_admin(x_admin_password: Optional[str] = Header(default=None)):
    if not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="Admin access is not configured. Set the ADMIN_PASSWORD "
                   "environment variable on the Railway web service.",
        )
    if not x_admin_password or not secrets.compare_digest(x_admin_password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Incorrect admin password.")
    return True

Base.metadata.create_all(bind=engine)

app = FastAPI(title="BikeHouston FAQ API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to bikehouston.org / your GitHub Pages domain before launch
    allow_methods=["*"],
    allow_headers=["*"],
)


def _display_date(entry) -> str:
    """
    The single date a reader sees: the most recent of created / updated /
    verified. Answers go stale — a route changes, a program ends — so showing
    when an entry was last touched lets someone judge for themselves whether
    it's still current, without us having to guess on their behalf.
    """
    candidates = [
        getattr(entry, "last_verified_at", None),
        getattr(entry, "updated_at", None),
        getattr(entry, "created_at", None),
    ]
    candidates = [c for c in candidates if c]
    return max(candidates).isoformat() if candidates else None


def _log_edit(db, entry, editor_name, action, previous_answer=None, note=None):
    """Append to the audit trail. Never updates in place — the history is the point."""
    db.add(models.FAQEditLog(
        faq_entry_id=entry.id,
        editor_name=(editor_name or "unknown")[:128],
        action=action,
        note=note,
        previous_answer=previous_answer,
        new_answer=entry.answer,
    ))


ESCALATION_NOTE = ("I can't answer this confidently, so I'm sending it to the "
                   "BikeHouston team for review.")


def _resolve(result: dict, default_reason: str):
    """
    Turn the AI's self-report into (escalated, reason, answer_text).

    needs_human is checked FIRST, before on_topic. That ordering matters: a
    question can be squarely on-topic for BikeHouston and still need a person —
    anything turning on Texas law, or on local judgement like "which intersection
    most needs a bike lane". Previously an off-topic verdict short-circuited
    escalation, so those questions were answered with a polite brush-off and
    never reached the review queue.

    An escalated answer can still carry useful practical content. If the model
    supplied one, show it and append the escalation note rather than throwing it
    away and replacing it with the note alone.
    """
    # escalation_reason is VARCHAR(128). The model writes prose here, and a
    # long reason used to blow up the INSERT with StringDataRightTruncation —
    # taking down the whole /ask request after the answer had been generated.
    # Truncate at the boundary rather than trusting the model to be brief.
    MAX_REASON = 128

    def _fit(reason: str) -> str:
        reason = (reason or "").strip()
        return reason[:MAX_REASON - 1] + "\u2026" if len(reason) > MAX_REASON else reason

    needs_human = bool(result.get("needs_human"))
    confident = bool(result.get("confident", False))
    on_topic = bool(result.get("on_topic", True))
    partial = (result.get("answer") or "").strip()

    if needs_human or (on_topic and not confident):
        reason = _fit(result.get("needs_human_reason")) or default_reason
        if partial:
            return True, reason, partial + "\n\n" + ESCALATION_NOTE
        return True, reason, ESCALATION_NOTE

    if not on_topic:
        return False, None, partial

    return False, None, partial


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
        escalated, escalation_reason, answer_text = _resolve(result, "low_confidence")

    else:
        # No good match — let the AI attempt cold, but default to escalation on any doubt
        result = ai.generate_answer(payload.query)
        escalated, escalation_reason, answer_text = _resolve(result, "no_kb_match")
        if not escalated and result.get("on_topic", True):
            confidence = "medium"

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

    # Only KB-sourced answers carry a date — a freshly generated one has no
    # entry behind it, so there's nothing meaningful to date it to.
    last_updated = _display_date(match) if (match and matched_entry_id) else None

    return schemas.AskResponse(
        last_updated=last_updated,
        answer=answer_text,
        escalated=escalated,
        escalation_reason=escalation_reason,
        matched_entry_id=matched_entry_id,
        confidence=confidence,
        query_id=log_entry.id,
    )


@app.post("/query/{query_id}/email")
def attach_email(query_id: int, payload: schemas.EmailRequest, db: Session = Depends(get_db)):
    """
    Public endpoint: lets someone whose question was escalated leave an email
    afterwards, so a human can follow up. Sending is manual — see the admin
    dashboard, which surfaces the address with a copy button.
    """
    q = db.query(models.UserQuery).filter(models.UserQuery.id == query_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="not found")
    email = (payload.email or "").strip()
    if "@" not in email or len(email) > 255:
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    q.asker_email = email
    db.commit()
    return {"ok": True}


@app.post("/query/{query_id}/feedback")
def query_feedback(query_id: int, helpful: bool, db: Session = Depends(get_db)):
    """
    Public endpoint: thumbs up/down on the answer someone actually received.
    Votes attach to the logged query rather than the KB entry, so freshly
    AI-generated answers are votable too — that's the whole point, since an
    unhelpful vote on an uncurated answer is the most useful signal we get.
    """
    q = db.query(models.UserQuery).filter(models.UserQuery.id == query_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="not found")
    if helpful:
        q.helpful_count = (q.helpful_count or 0) + 1
    else:
        q.unhelpful_count = (q.unhelpful_count or 0) + 1
    db.commit()
    return {"helpful_count": q.helpful_count, "unhelpful_count": q.unhelpful_count}


@app.get("/popular")
def popular_questions(limit: int = 10, db: Session = Depends(get_db)):
    """
    Public endpoint: the most-asked questions, ranked by how often each
    published KB entry has been matched. Powers the 'Most Asked' section
    on the frontend. Only surfaces already-published FAQ content — never
    raw user-typed query text — so it's safe to expose without auth.
    """
    # The published filter has to be applied BEFORE the limit, not after.
    # Ranking first and filtering second meant the top N could be entirely
    # unpublished entries — which is exactly what happened after the legal
    # audit pulled the six most-asked seeds. The list came back nearly empty
    # not because nothing qualified, but because nothing qualifying was ever
    # examined.
    rows = (
        db.query(
            models.FAQEntry.id,
            models.FAQEntry.question_canonical,
            models.FAQEntry.answer,
            func.count(models.UserQuery.id).label("ask_count"),
        )
        .outerjoin(models.UserQuery, models.UserQuery.matched_entry_id == models.FAQEntry.id)
        .filter(models.FAQEntry.status == "published")
        .group_by(models.FAQEntry.id, models.FAQEntry.question_canonical, models.FAQEntry.answer)
        .order_by(desc("ask_count"), models.FAQEntry.id)
        .limit(limit)
        .all()
    )

    # outerjoin so a freshly promoted entry with no matches yet still appears
    # rather than being invisible until someone happens to ask it.
    ids = [r[0] for r in rows]
    dates = {}
    if ids:
        for e in db.query(models.FAQEntry).filter(models.FAQEntry.id.in_(ids)).all():
            dates[e.id] = _display_date(e)
    return [
        {"id": r[0], "question": r[1], "answer": r[2], "ask_count": r[3],
         "last_updated": dates.get(r[0])}
        for r in rows
    ]


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
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "last_updated": _display_date(e),
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

@app.get("/admin/stats", dependencies=[Depends(require_admin)])
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

    # Customer satisfaction: of the people who bothered to vote, what share
    # said the answer helped? Deliberately counts votes, not questions —
    # most people never vote, and that's fine.
    helpful_votes = db.query(func.coalesce(func.sum(models.UserQuery.helpful_count), 0)).scalar() or 0
    unhelpful_votes = db.query(func.coalesce(func.sum(models.UserQuery.unhelpful_count), 0)).scalar() or 0
    total_votes = helpful_votes + unhelpful_votes

    return {
        "helpful_votes": helpful_votes,
        "unhelpful_votes": unhelpful_votes,
        "total_votes": total_votes,
        "satisfaction_pct": round(100 * helpful_votes / total_votes, 1) if total_votes else None,
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


@app.get("/admin/queries", dependencies=[Depends(require_admin)])
def list_queries(limit: int = 50, db: Session = Depends(get_db)):
    rows = (
        db.query(models.UserQuery)
        .order_by(desc(models.UserQuery.created_at))
        .limit(limit)
        .all()
    )
    return rows


@app.get("/admin/review-queue", response_model=List[schemas.ReviewQueueOut], dependencies=[Depends(require_admin)])
def list_review_queue(status: str = "open", db: Session = Depends(get_db)):
    rows = (
        db.query(models.ReviewQueueItem)
        .filter(models.ReviewQueueItem.status == status)
        .order_by(desc(models.ReviewQueueItem.flagged_at))
        .all()
    )
    return rows


@app.post("/admin/review-queue/{item_id}/resolve", dependencies=[Depends(require_admin)])
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


@app.get("/admin/faq", response_model=List[schemas.FAQEntryOut], dependencies=[Depends(require_admin)])
def list_faq(db: Session = Depends(get_db)):
    return db.query(models.FAQEntry).order_by(models.FAQEntry.category).all()


@app.post("/admin/faq", response_model=schemas.FAQEntryOut, dependencies=[Depends(require_admin)])
def create_faq(entry: schemas.FAQEntryIn, db: Session = Depends(get_db)):
    db_entry = models.FAQEntry(**entry.dict())
    db.add(db_entry)
    db.commit()
    db.refresh(db_entry)
    return db_entry


@app.put("/admin/faq/{entry_id}", response_model=schemas.FAQEntryOut, dependencies=[Depends(require_admin)])
def update_faq(entry_id: int, entry: schemas.FAQEntryIn, db: Session = Depends(get_db)):
    db_entry = db.query(models.FAQEntry).filter(models.FAQEntry.id == entry_id).first()
    if not db_entry:
        return {"error": "not found"}
    for k, v in entry.dict().items():
        setattr(db_entry, k, v)
    db.commit()
    db.refresh(db_entry)
    return db_entry


@app.get("/admin/flagged", dependencies=[Depends(require_admin)])
def admin_flagged(db: Session = Depends(get_db)):
    """
    Published KB entries that have picked up at least one thumbs-down.
    A downvote IS the flag — there's no separate 'report' control, so this
    is the review list: worst ratio first.
    """
    entries = (
        db.query(models.FAQEntry)
        .filter(models.FAQEntry.unhelpful_count > 0)
        .all()
    )
    result = [
        {
            "id": e.id,
            "question": e.question_canonical,
            "answer": e.answer,
            "category": e.category,
            "helpful_count": e.helpful_count or 0,
            "unhelpful_count": e.unhelpful_count or 0,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "last_updated": _display_date(e),
        }
        for e in entries
    ]
    result.sort(key=lambda r: (-r["unhelpful_count"], r["helpful_count"]))
    return result


# ---- Legal-claim screening for promotion ----
# Rounds 3-5 hardened the generation path: the AI can no longer cite statutes or
# assert legality, and legal questions escalate. Promotion had no equivalent
# check, and every answer logged before round 3 predates those rules — so a
# single click could reintroduce exactly what the legal audit removed. It did:
# entry 10 went public with a fabricated section number after the audit.
#
# This screens the text rather than trusting the era it was written in.
import re as _re

_STATUTE_PATTERNS = [
    (_re.compile(r"§"), "section symbol"),
    (_re.compile(r"\b\d{3}\.\d{2,3}\b"), "statute-style number"),
    (_re.compile(r"\bSec(?:tion|\.)\s*\d", _re.I), "section reference"),
    (_re.compile(r"\b(?:Transportation|Penal|Health\s+and\s+Safety|Local\s+Government)\s+Code\b", _re.I), "code reference"),
    (_re.compile(r"\bChapter\s+\d+\b", _re.I), "chapter reference"),
]

_LEGALITY_PATTERNS = [
    (_re.compile(r"\bis\s+(?:it\s+)?(?:legal|illegal)\b", _re.I), "legality claim"),
    (_re.compile(r"\b(?:it'?s|that'?s)\s+(?:legal|illegal)\b", _re.I), "legality claim"),
    (_re.compile(r"\b(?:are|is)\s+(?:not\s+)?(?:required|permitted|prohibited|allowed)\s+by\s+law\b", _re.I), "legal requirement"),
    (_re.compile(r"\brequired\s+by\s+(?:Texas|Houston|state|city)\s+law\b", _re.I), "legal requirement"),
    (_re.compile(r"\b(?:Texas|Houston)\s+law\s+(?:requires|allows|permits|prohibits|states)\b", _re.I), "legal assertion"),
    (_re.compile(r"\bagainst\s+the\s+law\b", _re.I), "legality claim"),
    (_re.compile(r"\blegally\s+(?:allowed|permitted|required|entitled)\b", _re.I), "legality claim"),
    (_re.compile(r"\bhelmets?\s+(?:are|is)\s+required\b", _re.I), "helmet requirement claim"),
]

# The patterns above all key on affirmative phrasing — "law requires", "is
# legal" — so the negative forms slipped straight through: "Texas law doesn't
# require...", "there's no state law requiring...", "riding on the roadway is a
# legal right". That is the exact text of entry 4, which the audit had to
# unpublish, so this is a demonstrated gap rather than a hypothetical one.
#
# Telling a rider they are under no obligation is as actionable, and as
# wrong-able, as telling them something is required — a negated claim is not the
# safer half of the pair.
_NEGATED_LEGALITY_PATTERNS = [
    (_re.compile(r"\b(?:no|not|never)\s+(?:\w+\s+){0,3}(?:law|statute|ordinance)s?\b", _re.I), "negated legal claim"),
    (_re.compile(r"\b(?:law|statute|ordinance)s?\s+(?:does|do|did)\s*n[o']?t\b", _re.I), "negated legal claim"),
    (_re.compile(r"\b(?:law|statute|ordinance)s?\s+(?:does|do|did)\s+not\b", _re.I), "negated legal claim"),
    (_re.compile(r"\bnot\s+(?:required|obligated|obliged|permitted|allowed)\s+to\b", _re.I), "negated legal claim"),
    (_re.compile(r"\b(?:a|an|the|your|their|no)\s+legal\s+(?:right|obligation|duty|requirement)\b", _re.I), "legality claim"),
    (_re.compile(r"\blawful(?:ly)?\b", _re.I), "legality claim"),
    (_re.compile(r"\b(?:police|officers?|cops?)\s+(?:can|could|may|will)\s*n[o']?t\b", _re.I), "enforcement claim"),
    (_re.compile(r"\b(?:police|officers?|cops?)\s+cannot\b", _re.I), "enforcement claim"),
]


def screen_for_legal_claims(text: str):
    """Return a list of (kind, snippet) for anything that reads as a legal claim."""
    findings = []
    if not text:
        return findings
    for pattern, kind in _STATUTE_PATTERNS + _LEGALITY_PATTERNS + _NEGATED_LEGALITY_PATTERNS:
        for m in pattern.finditer(text):
            start = max(0, m.start() - 45)
            end = min(len(text), m.end() + 45)
            findings.append({"kind": kind, "match": m.group(0), "context": text[start:end].strip()})
    return findings


@app.post("/admin/promote/{query_id}", dependencies=[Depends(require_admin)])
def promote_query(query_id: int, category: str = "general", force: bool = False, db: Session = Depends(get_db)):
    """
    Promote a logged question+answer out of user_queries and into faq_entries,
    so it becomes browsable, votable, and — importantly — a trigram match target
    for future questions, which means the next person asking something similar
    gets a direct KB answer with no AI call.
    """
    q = db.query(models.UserQuery).filter(models.UserQuery.id == query_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="not found")
    if not q.ai_answer or not q.ai_answer.strip():
        raise HTTPException(status_code=400, detail="That query has no answer to promote.")
    if q.was_escalated:
        raise HTTPException(
            status_code=400,
            detail="That question was escalated — answer it in the review queue instead.",
        )

    # Legal claims don't go into the knowledge base from the query log. They go
    # to BikeHouston staff. force=true exists so a human who has actually
    # verified the text can override — deliberately awkward, not a default.
    if not force:
        findings = screen_for_legal_claims(q.ai_answer)
        if findings:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": ("This answer makes a legal claim, so it can't be promoted directly. "
                                "Send it to BikeHouston staff to verify or rewrite."),
                    "findings": findings[:6],
                },
            )

    existing = (
        db.query(models.FAQEntry)
        .filter(models.FAQEntry.question_canonical == q.query_text)
        .first()
    )
    if existing:
        return {"ok": True, "entry_id": existing.id, "already_existed": True}

    entry = models.FAQEntry(
        question_canonical=q.query_text,
        question_variants=[],
        answer=q.ai_answer,
        category=category,
        sources=[],
        status="published",
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    # Point the originating query at the new entry so it starts with an
    # ask_count of 1 rather than showing "0 asked" on the browse page.
    q.matched_entry_id = entry.id
    db.commit()

    return {"ok": True, "entry_id": entry.id, "already_existed": False}


@app.post("/admin/review-queue/{item_id}/sent", dependencies=[Depends(require_admin)])
def mark_reply_sent(item_id: int, db: Session = Depends(get_db)):
    """Record that a human has emailed the answer back to the asker."""
    item = db.query(models.ReviewQueueItem).filter(models.ReviewQueueItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    item.reply_sent_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "reply_sent_at": item.reply_sent_at}


@app.get("/admin/verify", dependencies=[Depends(require_admin)])
def admin_verify():
    """Cheap endpoint for the admin page's password prompt to validate against."""
    return {"ok": True}


@app.post("/admin/faq/{entry_id}/status", dependencies=[Depends(require_admin)])
def set_faq_status(entry_id: int, status: str, db: Session = Depends(get_db)):
    """
    Change a KB entry's publication status without touching its content.

    Exists because PUT /admin/faq/{id} replaces every field from the request
    body, so using it to unpublish would blank anything not resent. This is the
    safe way to pull an entry off the public site — e.g. one making a legal
    claim that needs a human to verify — while keeping the text intact for
    whoever rewrites it.

    "published" is the only status /browse and /popular will show.
    """
    allowed = {"published", "draft", "needs_review"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(allowed)}")
    entry = db.query(models.FAQEntry).filter(models.FAQEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="not found")

    # Legal content can only reach the public site through /approve, which
    # records who signed off. Without this check the approval gate lives only in
    # the admin page's choice of button — one UI bug away from being bypassed
    # silently, which is precisely what happened: /admin/faq/all omitted
    # is_legal, so every entry rendered a plain Publish button and this endpoint
    # would have published legal text with nobody's name against it.
    if status == "published" and entry.is_legal and not entry.approved_by:
        raise HTTPException(
            status_code=409,
            detail=(
                "This entry contains legal content and has not been approved. "
                "Publish it via POST /admin/faq/{id}/approve?approver_name=... "
                "so the sign-off is recorded."
            ),
        )

    entry.status = status
    db.commit()
    return {"id": entry.id, "status": entry.status, "question": entry.question_canonical}


@app.get("/admin/faq/all", dependencies=[Depends(require_admin)])
def admin_faq_all(db: Session = Depends(get_db)):
    """Every KB entry regardless of status — the published-only /admin/faq
    listing can't show you what you've already pulled down for review."""
    entries = db.query(models.FAQEntry).order_by(models.FAQEntry.id).all()
    return [
        {
            "id": e.id,
            "question": e.question_canonical,
            "answer": e.answer,
            "category": e.category,
            "status": e.status,
            "helpful_count": e.helpful_count or 0,
            "unhelpful_count": e.unhelpful_count or 0,
            # The admin page renders the legal-content pill, the approval line,
            # the entry date, and — critically — decides between "Publish" and
            # "Approve & publish" from these. They were added to the model in
            # round 9 but not to this response, so the page saw undefined and
            # every entry got a plain Publish button.
            "is_legal": bool(e.is_legal),
            "approved_by": e.approved_by,
            "approved_at": e.approved_at.isoformat() if e.approved_at else None,
            "last_updated": _display_date(e),
        }
        for e in entries
    ]


# ---- Editing, legal approval, and staleness ----

@app.patch("/admin/faq/{entry_id}", dependencies=[Depends(require_admin)])
def edit_faq_entry(
    entry_id: int,
    editor_name: str,
    answer: Optional[str] = None,
    question: Optional[str] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Partial edit. Only the fields supplied are changed.

    Deliberately separate from PUT /admin/faq/{id}, which replaces every field
    from the request body — using that to change one line silently blanks
    everything not resent.

    Editing an entry that makes a legal claim clears its approval and pulls it
    off the public site. Otherwise a sign-off quietly transfers to text nobody
    checked, which is the failure mode an audit trail is supposed to prevent.
    """
    if not (editor_name or "").strip():
        raise HTTPException(status_code=400, detail="editor_name is required — edits are recorded.")

    entry = db.query(models.FAQEntry).filter(models.FAQEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="not found")

    previous = entry.answer
    changed = []

    if answer is not None and answer.strip() and answer != entry.answer:
        entry.answer = answer
        changed.append("answer")
    if question is not None and question.strip():
        entry.question_canonical = question
        changed.append("question")
    if category is not None and category.strip():
        entry.category = category
        changed.append("category")

    if not changed:
        return {"id": entry.id, "changed": [], "message": "Nothing changed."}

    note = "edited: " + ", ".join(changed)
    revoked = False

    # Re-screen: an edit can introduce a legal claim into an entry that didn't
    # have one, as well as remove one.
    findings = screen_for_legal_claims(entry.answer)
    if findings:
        entry.is_legal = True

    if entry.is_legal and "answer" in changed and entry.approved_by:
        entry.approved_by = None
        entry.approved_at = None
        if entry.status == "published":
            entry.status = "needs_review"
        revoked = True
        note += " — approval cleared, needs re-approval before republishing"

    entry.last_verified_at = datetime.now(timezone.utc)
    _log_edit(db, entry, editor_name, "edited", previous_answer=previous, note=note)
    db.commit()
    db.refresh(entry)

    return {
        "id": entry.id,
        "changed": changed,
        "status": entry.status,
        "is_legal": bool(entry.is_legal),
        "approval_revoked": revoked,
        "last_updated": _display_date(entry),
    }


@app.post("/admin/faq/{entry_id}/legal", dependencies=[Depends(require_admin)])
def set_legal_flag(entry_id: int, editor_name: str, is_legal: bool = True, db: Session = Depends(get_db)):
    """
    Mark an entry as containing legal content, without touching its text.

    is_legal is only ever set automatically on the paths that rewrite an entry —
    the edit re-screen and approval. That left no way to flag an entry that
    already exists: the only route was to edit it, which clears any approval and
    moves its date, falsely implying someone revised the answer. Entries the
    legal audit pulled down predate the column entirely and all default to
    false, so without this they would publish with no approval required.

    Clearing the flag also clears any approval — an approval is a sign-off on
    legal content, so it means nothing once the entry is no longer legal.
    """
    name = (editor_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="editor_name is required.")

    entry = db.query(models.FAQEntry).filter(models.FAQEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="not found")

    entry.is_legal = bool(is_legal)
    if not is_legal:
        entry.approved_by = None
        entry.approved_at = None

    _log_edit(
        db, entry, name,
        "flagged_legal" if is_legal else "unflagged_legal",
        note=("marked as legal content" if is_legal else "no longer marked as legal content"),
    )
    db.commit()
    db.refresh(entry)

    return {
        "id": entry.id,
        "is_legal": bool(entry.is_legal),
        "status": entry.status,
        "approved_by": entry.approved_by,
        "question": entry.question_canonical,
    }


@app.post("/admin/faq/{entry_id}/approve", dependencies=[Depends(require_admin)])
def approve_legal_entry(entry_id: int, approver_name: str, db: Session = Depends(get_db)):
    """
    Sign off on an entry making a legal claim, and publish it.

    The name is recorded and kept, but never shown to readers — the public sees
    only the date. This is the route back for the entries the legal audit pulled
    down: someone who knows the law confirms the text, puts their name to it,
    and it goes live.
    """
    name = (approver_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="approver_name is required.")

    entry = db.query(models.FAQEntry).filter(models.FAQEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="not found")

    entry.is_legal = True
    entry.approved_by = name[:128]
    entry.approved_at = datetime.now(timezone.utc)
    entry.last_verified_at = entry.approved_at
    entry.status = "published"

    _log_edit(db, entry, name, "approved", note="legal content approved and published")
    db.commit()
    db.refresh(entry)

    return {
        "id": entry.id,
        "status": entry.status,
        "approved_by": entry.approved_by,
        "last_updated": _display_date(entry),
    }


@app.get("/admin/faq/{entry_id}/history", dependencies=[Depends(require_admin)])
def faq_history(entry_id: int, db: Session = Depends(get_db)):
    """Every recorded change to this entry, newest first."""
    rows = (
        db.query(models.FAQEditLog)
        .filter(models.FAQEditLog.faq_entry_id == entry_id)
        .order_by(desc(models.FAQEditLog.created_at))
        .all()
    )
    return [
        {
            "id": r.id,
            "editor_name": r.editor_name,
            "action": r.action,
            "note": r.note,
            "previous_answer": r.previous_answer,
            "new_answer": r.new_answer,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@app.get("/admin/stale", dependencies=[Depends(require_admin)])
def stale_entries(days: int = 365, db: Session = Depends(get_db)):
    """
    Published entries not touched in `days`. Surfaces them for review only —
    nothing is unpublished automatically. Most answers don't go stale, and
    content silently vanishing is worse than old content carrying a visible date.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out = []
    for e in db.query(models.FAQEntry).filter(models.FAQEntry.status == "published").all():
        stamp = e.last_verified_at or e.updated_at or e.created_at
        if stamp and stamp < cutoff:
            out.append({
                "id": e.id,
                "question": e.question_canonical,
                "category": e.category,
                "is_legal": bool(e.is_legal),
                "last_updated": _display_date(e),
                "days_old": (datetime.now(timezone.utc) - stamp).days,
            })
    out.sort(key=lambda r: -r["days_old"])
    return out


@app.delete("/admin/queries/{query_id}", dependencies=[Depends(require_admin)])
def delete_query(query_id: int, db: Session = Depends(get_db)):
    """
    Remove a logged question from the query log.

    For pruning the log down to questions worth keeping — test runs, duplicates,
    nonsense, and answers superseded by a better one. This does NOT touch the
    knowledge base: if the question was promoted, that entry stays published and
    is unaffected.

    Any open review-queue item pointing at this query is removed too, since a
    queue entry whose question no longer exists can't be answered.
    """
    q = db.query(models.UserQuery).filter(models.UserQuery.id == query_id).first()
    if not q:
        raise HTTPException(status_code=404, detail="not found")

    removed_review_items = (
        db.query(models.ReviewQueueItem)
        .filter(models.ReviewQueueItem.related_query_id == query_id)
        .delete(synchronize_session=False)
    )
    db.delete(q)
    db.commit()
    return {"deleted": query_id, "review_items_removed": removed_review_items}


@app.get("/admin/questions", dependencies=[Depends(require_admin)])
def admin_questions(limit: int = 500, db: Session = Depends(get_db)):
    """
    One list: every question ever asked, joined to whatever knowledge base entry
    it produced and to any open review-queue item.

    Replaces the old split between "Recent questions", "Knowledge base", "Needs
    a human answer" and "Flagged by readers", which showed the same questions in
    up to three places at once. Status belongs on the row, not in the choice of
    section.

    Read-only. Does not modify anything, and deliberately leaves the existing
    /admin/queries, /admin/faq/all, /admin/review-queue and /admin/flagged
    endpoints alone so nothing already depending on them changes.
    """
    queries = (
        db.query(models.UserQuery)
        .order_by(desc(models.UserQuery.created_at))
        .limit(limit)
        .all()
    )

    entry_ids = {q.matched_entry_id for q in queries if q.matched_entry_id}
    entries = {}
    if entry_ids:
        for e in db.query(models.FAQEntry).filter(models.FAQEntry.id.in_(entry_ids)).all():
            entries[e.id] = e

    query_ids = [q.id for q in queries]
    open_reviews = {}
    if query_ids:
        for r in (
            db.query(models.ReviewQueueItem)
            .filter(
                models.ReviewQueueItem.related_query_id.in_(query_ids),
                models.ReviewQueueItem.status == "open",
            )
            .all()
        ):
            open_reviews[r.related_query_id] = r

    now = datetime.now(timezone.utc)
    out = []
    for q in queries:
        entry = entries.get(q.matched_entry_id) if q.matched_entry_id else None
        review = open_reviews.get(q.id)

        stale = False
        if entry and entry.status == "published":
            stamp = entry.last_verified_at or entry.updated_at or entry.created_at
            if stamp and (now - stamp).days > 365:
                stale = True

        out.append({
            "id": q.id,
            "question": q.query_text,
            "answer": q.ai_answer,
            "asked_at": q.created_at.isoformat() if q.created_at else None,
            "escalated": bool(q.was_escalated),
            "match_score": q.match_score,
            "helpful_count": q.helpful_count or 0,
            "unhelpful_count": q.unhelpful_count or 0,
            "asker_email": q.asker_email,
            "review_item_id": review.id if review else None,
            "entry": None if not entry else {
                "id": entry.id,
                "status": entry.status,
                "is_legal": bool(entry.is_legal),
                "approved_by": entry.approved_by,
                "last_updated": _display_date(entry),
                "helpful_count": entry.helpful_count or 0,
                "unhelpful_count": entry.unhelpful_count or 0,
            },
            "stale": stale,
        })
    return out
