# Gigi Growth Dashboard

TikTok (+ Instagram, pending handle) analytics dashboard for Gigi Chahal / She Creates Academy.
Live at https://plfranc0.github.io/gigi-dashboard/

- `scripts/pull.py` — pulls stats via Apify (Gigi's account), all TikTok via clockworks since 2026-09-06 (apidojo retired). Recent: 10 videos 3x/day (~$3.40/mo). Full sweep: whole catalog on the 1st + 15th (~$0.45/run). Follower counts are rounded to ~nearest 100 (clockworks limitation, accepted to stay on the free plan); video stats are exact.
- `.github/workflows/pull.yml` — 3x/day recent cron + 1st/15th full sweep + 2x/day IG. Needs `APIFY_TOKEN` secret.
- `assets/covers/*.jpg` — post thumbnails. The CDN cover URLs are signed and expire in ~48h, so they cannot be hotlinked: `pull.py` fetches each one once, downscales it to 320px wide (~26KB) and commits it. The per-video `cover` flag is recomputed from what is actually on disk every run, so it can never point at a missing file. A missing cover renders as a gradient tile, not a broken image.
- `index.html` — static dashboard, reads `data/*.json`. Thumbnails appear on the best/worst cards, in the Top posts grid, and on every row of the video table.

Managed from the Focal Point EA workspace (`clients/she-creates-academy/`). Built 2026-09-05.
