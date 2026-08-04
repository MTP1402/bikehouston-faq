from sqlalchemy import text
from sqlalchemy.orm import Session


def find_best_match(db: Session, query: str):
    """
    Uses pg_trgm similarity to compare the incoming query against each
    FAQ entry's canonical question and its stored variants, and returns
    the best-matching entry plus its similarity score (0-1).

    Requires the pg_trgm extension (see init.sql).
    """
    sql = text("""
        SELECT
            f.id,
            f.answer,
            f.status,
            GREATEST(
                similarity(f.question_canonical, :q),
                COALESCE(
                    (SELECT MAX(similarity(v, :q)) FROM unnest(f.question_variants) AS v),
                    0
                )
            ) AS score
        FROM faq_entries f
        WHERE f.status = 'published'
        ORDER BY score DESC
        LIMIT 1
    """)
    result = db.execute(sql, {"q": query}).fetchone()
    if result is None:
        return None, 0.0
    return result, result.score


# Confidence thresholds — tune these after watching real query traffic
HIGH_CONFIDENCE = 0.45   # answer directly from KB, no AI generation needed
MEDIUM_CONFIDENCE = 0.22  # let AI answer using nearby context, flag for review
# below MEDIUM_CONFIDENCE -> escalate to human
