# Reddit Scheduler (Flask + PRAW)

Features:
- Schedule **link, text, or image** posts to subreddits.
- Simple web UI.
- Stores jobs in SQLite.
- File upload for image posts (saved under `/data/uploads`).

## Env Vars
- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD` – Reddit "script" app + account creds.
- `APP_TIMEZONE` – How the form interprets your input times (default `UTC`), e.g. `America/Chicago`.
- `UPLOAD_DIR` – Defaults to `/data/uploads`.
- `FLASK_SECRET_KEY` – Set to a long random string in production.
- `MAX_UPLOAD_MB` – Max image size in MB (default 10).

## Run with Docker (local, build)
```bash
docker build -t reddit-scheduler .
docker run -d -p 5000:5000 \
  -e REDDIT_CLIENT_ID=your_id \
  -e REDDIT_CLIENT_SECRET=your_secret \
  -e REDDIT_USERNAME=your_user \
  -e REDDIT_PASSWORD=your_pass \
  -e APP_TIMEZONE=America/Chicago \
  -v reddit_scheduler_data:/data \
  reddit-scheduler
```

## Run with Docker Compose (no build)
This variant uses the `python:3.11-slim` image and installs deps at runtime. Place the app source into a volume mounted at `/app` (see `docker-compose.runtime.yml`).

## Notes
- Respect subreddit rules and Reddit API policies. Many subreddits restrict images/self-promo.
- Image posts require the target subreddit to allow images.
