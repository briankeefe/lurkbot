# LurkBot

Reddit subreddit scanner for finding organic marketing opportunities and product validation signals.

## What it does

LurkBot watches a set of subreddits for posts matching business pain signals:

- **Pain signals** — "frustrated", "wish there was", "anyone know a tool"
- **Manual process signals** — "spreadsheet", "excel", "copy paste", "manually track"
- **Tool complaints** — "too expensive", "missing feature", "switching from", "alternative to"

Posts are scored by signal match count, upvotes, and comment count, then surfaced in a ranked feed. The idea: find people already talking about the problem you solve, reach out in context instead of cold.

## Use case

Built for validating [heedline](https://github.com/briankeefe/heedline) — a reputation management + scheduler integration SaaS. Monitors subreddits like r/smallbusiness, r/salon, r/agency for pain signals around reviews, reputation, and scheduling.

## Stack

- Python + FastAPI
- SQLite
- APScheduler (scans every 6 hours)
- Raw Reddit JSON API (no auth needed)
- No LLM involved — pure keyword/signal matching

## Running

```bash
docker compose up -d
```

Web UI at http://localhost:5200

## Config

Edit `config.json` to change subreddits, keywords, and signal patterns.

```json
{
  "subreddits": ["smallbusiness", "salon", "agency", ...],
  "keywords": ["reviews", "reputation", "scheduling", ...],
  "scan_interval_hours": 6
}
```

## API

- `GET /` — ranked feed UI
- `GET /api/feed` — JSON feed
- `POST /api/scan` — trigger manual scan
- `GET /health` — health check
