# Gigi Growth Dashboard

TikTok (+ Instagram, pending handle) analytics dashboard for Gigi Chahal / She Creates Academy.
Live at https://plfranc0.github.io/gigi-dashboard/

- `scripts/pull.py` — pulls stats via Apify (Gigi's account). Hourly: apidojo (10 recent, ~$0.003/run). Weekly full sweep: clockworks (whole catalog, ~$0.45/run). ~$4/mo total, fits Apify free tier.
- `.github/workflows/pull.yml` — hourly cron + Sunday full sweep. Needs `APIFY_TOKEN` secret.
- `index.html` — static dashboard, reads `data/*.json`.

Managed from the Focal Point EA workspace (`clients/she-creates-academy/`). Built 2026-09-05.
