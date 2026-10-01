# Keep-alive pinger (external, 5-minute interval)

Free-tier hosts sleep the server after ~15 min with no traffic, so the
first real visitor pays a cold start (process boot + Whisper warm-up).
The fix is an off-site cron that hits the health endpoint every 5 min.
Nothing runs in-app: no JS, no new route, no session or stats side
effects.

## Target

- URL: `https://<your-domain>/healthz`
- Expected: `200` with body `{"ok": true, "auth_enabled": ...}`
- Why this endpoint: public (in `PUBLIC_PATHS`, no login redirect), tiny
  JSON, no DB/model work. Answers `HEAD` as well as `GET`.

## Setup (pick one — the cron lives off-site, nothing to maintain here)

**UptimeRobot (recommended, free):**
1. Add Monitor -> Monitor Type: HTTP(s), Friendly Name: `autoquence`.
2. URL: `https://<your-domain>/healthz`, Monitoring Interval: 5 Minutes.
3. Enable "Alert Contacts" so you are emailed if it ever goes down
   (the pinger doubling as uptime monitoring is the point).

**cron-job.org (free):**
1. Create cronjob, URL: `https://<your-domain>/healthz`, schedule:
   every 5 minutes.
2. Turn on notification on failure.

## Cost

- 1 req / 5 min = 288 req/day against gunicorn `--max-requests 2000`
  (see Procfile), so pinger traffic alone recycles the worker roughly
  weekly instead of fighting the warm-up. Real traffic recycles sooner;
  that is fine — recycling is leak insurance, the pinger re-warms after.
- If the pinger itself goes down, the server can sleep again. The
  provider's down-alert covers this: no alert + no traffic = check the
  pinger, not the app.
