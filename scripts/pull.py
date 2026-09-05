#!/usr/bin/env python3
"""Pull Gigi's TikTok stats via Apify and merge into data/.

Modes (env MODE):
  hourly  - apidojo/tiktok-scraper, 10 most recent videos + profile stats (~$0.003/run)
  full    - clockworks/tiktok-scraper, entire catalog (~$0.45/run, weekly)

Data files written:
  data/videos.json      merged per-video latest stats
  data/profile.json     follower history (one entry per run) + current
  data/timeseries.json  per-video daily view counts {videoId: {date: views}}
  data/meta.json        lastUpdated / lastFullSweep
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

TOKEN = os.environ.get("APIFY_TOKEN")
if not TOKEN:
    sys.exit("APIFY_TOKEN not set")

MODE = os.environ.get("MODE", "hourly")
HANDLE = "gigichahal"
DATA = os.path.join(os.path.dirname(__file__), "..", "data")


def apify(actor, payload, timeout=280):
    url = f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items?timeout={timeout}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout + 20) as r:
        body = json.load(r)
    if not isinstance(body, list):
        sys.exit(f"unexpected apify response: {str(body)[:300]}")
    return body


def load(name, default):
    path = os.path.join(DATA, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save(name, obj):
    path = os.path.join(DATA, name)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def norm_apidojo(item):
    ch = item.get("channel") or {}
    vid = item.get("video") or {}
    return {
        "id": str(item["id"]),
        "url": item.get("postPage") or f"https://www.tiktok.com/@{HANDLE}/video/{item['id']}",
        "caption": item.get("title") or "",
        "createTime": item.get("uploadedAtFormatted") or "",
        "duration": vid.get("duration"),
        "views": item.get("views") or 0,
        "likes": item.get("likes") or 0,
        "comments": item.get("comments") or 0,
        "shares": item.get("shares") or 0,
        "saves": item.get("bookmarks") or 0,
        "hashtags": item.get("hashtags") or [],
    }, ch


def norm_clockworks(item):
    vm = item.get("videoMeta") or {}
    return {
        "id": str(item["id"]),
        "url": item.get("webVideoUrl") or f"https://www.tiktok.com/@{HANDLE}/video/{item['id']}",
        "caption": item.get("text") or "",
        "createTime": item.get("createTimeISO") or "",
        "duration": vm.get("duration"),
        "views": item.get("playCount") or 0,
        "likes": item.get("diggCount") or 0,
        "comments": item.get("commentCount") or 0,
        "shares": item.get("shareCount") or 0,
        "saves": item.get("collectCount") or 0,
        "hashtags": [h.get("name") for h in (item.get("hashtags") or []) if isinstance(h, dict) and h.get("name")],
    }


now = datetime.now(timezone.utc)
now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
today = now.strftime("%Y-%m-%d")

profile_stats = None
if MODE == "full":
    raw = apify(
        "clockworks~tiktok-scraper",
        {
            "profiles": [HANDLE],
            "resultsPerPage": 200,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "shouldDownloadSubtitles": False,
        },
    )
    videos = [norm_clockworks(v) for v in raw if v.get("id")]
    # clockworks reports ROUNDED follower counts (34500 vs apidojo's exact 34529);
    # mixing them into the history creates fake jumps, so full mode never touches
    # profile stats - the hourly apidojo runs keep that series clean.
else:
    raw = apify(
        "apidojo~tiktok-scraper",
        {"startUrls": [f"https://www.tiktok.com/@{HANDLE}"], "maxItems": 10},
    )
    pairs = [norm_apidojo(v) for v in raw if v.get("id")]
    videos = [p[0] for p in pairs]
    for _, ch in pairs:
        if ch.get("followers"):
            profile_stats = {
                "followers": ch.get("followers"),
                "following": ch.get("following"),
                "totalVideos": ch.get("videos"),
            }
            break

if not videos:
    sys.exit("scrape returned 0 videos - refusing to write")

# merge videos
store = load("videos.json", {})
for v in videos:
    prev = store.get(v["id"], {})
    prev.update(v)
    prev["lastSeen"] = now_iso
    store[v["id"]] = prev
save("videos.json", store)

# per-video daily view snapshots (latest value wins within a day)
ts = load("timeseries.json", {})
for v in videos:
    ts.setdefault(v["id"], {})[today] = v["views"]
save("timeseries.json", ts)

# follower history
prof = load("profile.json", {"history": [], "current": {}})
if profile_stats:
    prof["current"] = {**profile_stats, "at": now_iso}
    prof["history"].append({"at": now_iso, "followers": profile_stats["followers"]})
save("profile.json", prof)

meta = load("meta.json", {})
meta["lastUpdated"] = now_iso
meta["mode"] = MODE
if MODE == "full":
    meta["lastFullSweep"] = now_iso
meta["handle"] = HANDLE
save("meta.json", meta)

print(f"OK mode={MODE} videos={len(videos)} store={len(store)} followers={(profile_stats or {}).get('followers')}")
