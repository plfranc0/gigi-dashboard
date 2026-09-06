# Gigi Growth Dashboard

TikTok (+ Instagram, pending handle) analytics dashboard for Gigi Chahal / She Creates Academy.
Live at https://plfranc0.github.io/gigi-dashboard/

- `scripts/pull.py` — pulls stats via Apify (Gigi's account), all TikTok via clockworks since 2026-09-06 (apidojo retired). Recent: 10 videos 3x/day (~$3.40/mo). Full sweep: whole catalog on the 1st + 15th (~$0.45/run). Follower counts are rounded to ~nearest 100 (clockworks limitation, accepted to stay on the free plan); video stats are exact.
- `.github/workflows/pull.yml` — 3x/day recent cron + 1st/15th full sweep + 2x/day IG. Needs `APIFY_TOKEN` secret.
- `index.html` — static dashboard, reads `data/*.json`.

Managed from the Focal Point EA workspace (`clients/she-creates-academy/`). Built 2026-09-05.
