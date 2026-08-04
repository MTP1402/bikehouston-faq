# BikeHouston AI FAQ

AI-enhanced FAQ for bikehouston.org. Answers cycling/Houston-infrastructure questions from a
curated, human-reviewed knowledge base; escalates anything it's not confident about to a
human reviewer instead of guessing.

- **Backend:** FastAPI + Postgres, deployed on Railway
- **Frontend:** static HTML chat widget, deployed on GitHub Pages
- **AI:** Claude (Anthropic API), scoped by a strict system prompt to cycling/Houston topics only

## How it works

1. A question comes in → Postgres trigram similarity search checks it against the curated
   `faq_entries` table (handles paraphrasing reasonably well without needing embeddings yet).
2. **High similarity match** → answer served directly from the knowledge base, no AI generation.
3. **Partial match** → Claude answers, grounded in the closest KB entry, but only if confident.
4. **No good match / low confidence** → escalated to the review queue instead of guessing.
5. Every question is logged (`user_queries`) so you can see what people are actually asking.
6. Anything escalated lands in `review_queue` for a human (you/Mitch/Joe) to answer and turn
   into a permanent KB entry.

## Local setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in DATABASE_URL and ANTHROPIC_API_KEY

# enable required Postgres extension (once per database)
psql $DATABASE_URL -f init.sql

# load the starter knowledge base (Mitch's 4 questions)
python seed_data.py

# run locally
uvicorn app.main:app --reload
```

Visit `http://localhost:8000/docs` for interactive API docs.

## Deploying to Railway

1. Create a new Railway project → **Add a Postgres service** first.
2. Add a second service from this GitHub repo (backend).
3. Railway auto-injects `DATABASE_URL` from the Postgres service — you just need to add
   `ANTHROPIC_API_KEY` in the backend service's Variables tab.
4. Railway will use the `Procfile` automatically to start the app.
5. Once deployed, run the extension + seed steps against the Railway database:
   ```bash
   psql <railway-database-url> -f init.sql
   DATABASE_URL=<railway-database-url> python seed_data.py
   ```
6. Copy your Railway app URL (e.g. `https://bikehouston-faq-production.up.railway.app`).

## Deploying the frontend (GitHub Pages)

1. Open `frontend/index.html` and set `API_BASE` to your Railway URL.
2. Enable GitHub Pages on this repo, pointed at the `frontend/` folder (or `main` branch,
   `/frontend` path — same pattern as the cycling dashboard).
3. Before this goes anywhere public, tighten the CORS `allow_origins` in `app/main.py` from
   `"*"` to your actual GitHub Pages domain.

## Admin endpoints (no auth yet — add before going live)

- `GET /admin/queries` — recent questions asked, matches, escalations
- `GET /admin/review-queue?status=open` — questions needing a human answer
- `POST /admin/review-queue/{id}/resolve` — mark handled
- `GET /admin/faq` / `POST /admin/faq` / `PUT /admin/faq/{id}` — manage the knowledge base

## Not built yet (intentionally deferred)

- **Auth** on admin routes — needs to happen before this is public
- **Freshness re-check job** — a scheduled task that re-searches volatile entries (laws,
  stats) on their `recheck_interval_days` and flags diffs into `review_queue`. v1 ships
  without this; add once the KB has enough entries to be worth automating.
- **Semantic (embedding) search** — trigram matching is the v1 approach; revisit with
  pgvector if paraphrase matching proves too weak in practice.
