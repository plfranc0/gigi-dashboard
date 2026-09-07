#!/usr/bin/env python3
"""Pull Gigi's TikTok stats via Apify and merge into data/.

Modes (env MODE):
  recent  - clockworks/tiktok-scraper, 10 most recent videos + profile stats
            (~$0.037/run, 3x/day = ~$3.40/mo). Was apidojo hourly until 2026-09-06.
            NOTE: clockworks follower counts are ROUNDED (~nearest 100) - accepted
            trade-off to stay on the free plan; video stats stay exact.
  full    - clockworks/tiktok-scraper, entire catalog (~$0.45/run, 1st + 15th)
  ig      - Instagram. With IG_TOKEN set (the 60-day Graph API token, connected
            2026-09-07) this reads the OFFICIAL API: every reel regardless of
            feed-sharing, plus views/reach/likes/saves/shares per post - data the
            public page hides. Without IG_TOKEN it falls back to the old public
            scrape (8 photos, no numbers) and says so loudly.
            NOTE: Graph media ids are a different id space than the scraper's, so
            the first Graph run starts ig-videos/ig-timeseries fresh (the 2 days
            of scraper history were 8 photo posts with no counts - nothing lost).

Data files written:
  data/videos.json      merged per-video latest stats
  data/profile.json     follower history (one entry per run) + current
  data/timeseries.json  per-video daily view counts {videoId: {date: views}}
  data/meta.json        lastUpdated / lastFullSweep
  assets/covers/*.jpg   post thumbnails, downloaded once and committed

Covers: the CDN cover URLs are signed and expire in ~48h, so they cannot be
hotlinked from the dashboard. Each cover is fetched once, downscaled to 320px
wide (~26KB) and committed to the repo. The per-video `cover` flag is recomputed
from what is actually on disk every run, so it can never claim an image exists
when it does not. A cover failure never blocks the stats write.
"""
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from PIL import Image

TOKEN = os.environ.get("APIFY_TOKEN")
if not TOKEN:
    sys.exit("APIFY_TOKEN not set")

MODE = os.environ.get("MODE", "recent")
HANDLE = "gigichahal"
HANDLE_IG = "gigichahal_"
ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")
COVERS = os.path.join(ROOT, "assets", "covers")

COVER_W = 320       # display is <=200px wide, so 320 covers retina without bloating the repo
COVER_Q = 78
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


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


def norm_clockworks(item):
    vm = item.get("videoMeta") or {}
    return {
        "_cover": vm.get("coverUrl"),
        "_subs": vm.get("subtitleLinks") or [],
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


GRAPH = "https://graph.instagram.com/v23.0"
IG_INSIGHT_METRICS = "views,reach,likes,comments,saved,shares,total_interactions"
IG_INSIGHTS_CAP = 150   # per-media insights calls per run; platform limit is ~200/user/hour


def graph_get(path, **params):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{GRAPH}{path}?{qs}" if not path.startswith("http") else f"{path}&{qs}",
                                 headers={"User-Agent": "gigi-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def scrape_ig_graph(token):
    """Official Graph API pull: profile + all media (paged) + per-media insights."""
    me = graph_get("/me", fields="username,followers_count,follows_count,media_count",
                   access_token=token)
    profile_stats = {"followers": me["followers_count"], "following": me.get("follows_count"),
                     "totalVideos": me.get("media_count")}

    media, url = [], None
    fields = ("id,caption,media_type,media_product_type,permalink,thumbnail_url,"
              "media_url,timestamp,like_count,comments_count")
    page = graph_get("/me/media", fields=fields, limit=100, access_token=token)
    while True:
        media += page.get("data", [])
        nxt = (page.get("paging") or {}).get("next")
        if not nxt or len(media) > 1000:
            break
        page = graph_get(nxt)

    videos, ins_fail = [], []
    for i, m in enumerate(media):
        ins = {}
        if i < IG_INSIGHTS_CAP:
            try:
                res = graph_get(f"/{m['id']}/insights", metric=IG_INSIGHT_METRICS,
                                access_token=token)
                ins = {d["name"]: d["values"][0]["value"] for d in res.get("data", [])}
            except Exception as e:                               # noqa: BLE001
                ins_fail.append((m["id"], f"{type(e).__name__}: {e}"))
        is_reel = m.get("media_product_type") == "REELS"
        videos.append({
            "_cover": m.get("thumbnail_url") or m.get("media_url"),
            "id": str(m["id"]),
            "url": m.get("permalink") or "",
            "caption": m.get("caption") or "",
            "createTime": m.get("timestamp") or "",
            "duration": None,                       # Graph API does not expose duration
            "type": "Reel" if is_reel else m.get("media_type"),
            "views": ins.get("views"),
            "reach": ins.get("reach"),
            "likes": ins.get("likes", m.get("like_count")),
            "comments": ins.get("comments", m.get("comments_count")) or 0,
            "shares": ins.get("shares"),
            "saves": ins.get("saved"),
            "hashtags": [w[1:] for w in (m.get("caption") or "").split() if w.startswith("#")],
        })
    if len(media) > IG_INSIGHTS_CAP:
        print(f"::warning title=IG insights capped::{len(media)} media but insights fetched "
              f"for newest {IG_INSIGHTS_CAP} only (rate-limit headroom)")
    profile_stats["totalVideos"] = len(media)   # media_count only counts GRID posts (7 vs 112)
    if ins_fail:
        print(f"  {len(ins_fail)} media returned no insights (stored without):")
        for mid, why in ins_fail[:5]:
            print(f"    {mid}: {why[:120]}")
    print(f"  graph: {len(media)} media "
          f"({sum(1 for v in videos if v['type'] == 'Reel')} reels), "
          f"{me['followers_count']} followers")
    return videos, profile_stats


def norm_ig(item):
    likes = item.get("likesCount")
    return {
        "_cover": item.get("displayUrl"),
        "id": str(item["id"]),
        "url": item.get("url") or "",
        "caption": item.get("caption") or "",
        "createTime": item.get("timestamp") or "",
        "duration": item.get("videoDuration"),
        "type": item.get("type"),
        "views": item.get("videoPlayCount"),
        "likes": likes if likes is not None and likes >= 0 else None,
        "comments": item.get("commentsCount") or 0,
        "shares": None,
        "saves": None,
        "hashtags": item.get("hashtags") or [],
    }


now = datetime.now(timezone.utc)
now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
today = now.strftime("%Y-%m-%d")

prefix = "ig-" if MODE == "ig" else ""


def cover_path(vid_id):
    return os.path.join(COVERS, f"{prefix}{vid_id}.jpg")


def fetch_cover(url, dest):
    """Download one cover, downscale, write atomically. Raises on any problem."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read()
    if len(raw) < 1024:
        raise ValueError(f"suspiciously small response ({len(raw)} bytes) - likely an error page")
    im = Image.open(io.BytesIO(raw))
    im.load()                      # force decode now so a truncated file fails here, not later
    im = im.convert("RGB")
    w, h = im.size
    if w > COVER_W:
        im = im.resize((COVER_W, max(1, round(h * COVER_W / w))), Image.LANCZOS)
    tmp = dest + ".tmp"
    im.save(tmp, "JPEG", quality=COVER_Q, optimize=True, progressive=True)
    os.replace(tmp, dest)          # atomic - a half-written jpg never becomes the real file
    return os.path.getsize(dest)


def parse_vtt(vtt):
    """WebVTT -> plain text. TikTok tracks are word-fragments, so join and re-space."""
    words = []
    for line in vtt.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or "-->" in s or s.isdigit():
            continue
        words.append(s)
    return " ".join(" ".join(words).split())


def sync_transcripts(videos):
    """Fetch subtitle tracks for videos we haven't stored yet (TikTok modes only).

    Like cover urls, subtitle downloadLinks are signed and expire in ~48h, so
    the text is captured at pull time and committed. A video checked and found
    to have no track is stored as {"t": null} so it is never re-fetched.
    """
    store = load("transcripts.json", {})
    new = none = skipped = 0
    failed = []
    for v in videos:
        vid = v["id"]
        if vid in store:
            skipped += 1
            continue
        subs = v.get("_subs") or []
        track = next((s for s in subs if (s.get("language") or "").startswith("eng")), None)
        if not track or not track.get("downloadLink"):
            store[vid] = {"t": None}
            none += 1
            continue
        try:
            req = urllib.request.Request(track["downloadLink"], headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                text = parse_vtt(r.read().decode("utf-8", errors="replace"))
            if not text:
                raise ValueError("track downloaded but parsed to empty text")
            store[vid] = {"t": text}
            new += 1
        except Exception as e:                                   # noqa: BLE001
            failed.append((vid, f"{type(e).__name__}: {e}"))     # no store entry -> retried next run
    save("transcripts.json", store)
    have = sum(1 for x in store.values() if x.get("t"))
    print(f"transcripts: {new} new, {none} no-track, {skipped} cached, {len(failed)} failed | {have}/{len(store)} have text")
    for vid, why in failed:
        print(f"  TRANSCRIPT FAIL {vid}: {why}", flush=True)
    if failed:
        print(f"::warning title=Transcript downloads failed::{len(failed)} transcripts failed in mode={MODE}")


def sync_covers(videos, store):
    """Fetch any cover we don't already have, then set each `cover` flag from disk truth."""
    os.makedirs(COVERS, exist_ok=True)
    new = skipped = 0
    failed = []
    for v in videos:
        dest = cover_path(v["id"])
        if os.path.exists(dest):
            skipped += 1
            continue
        url = v.get("_cover")
        if not url:
            failed.append((v["id"], "no cover url in payload"))
            continue
        try:
            fetch_cover(url, dest)
            new += 1
        except Exception as e:                                   # noqa: BLE001
            failed.append((v["id"], f"{type(e).__name__}: {e}"))
            if os.path.exists(dest + ".tmp"):
                os.remove(dest + ".tmp")

    # recompute every flag from what is actually on disk - self-healing, and it
    # cannot report an image the dashboard would then 404 on
    have = 0
    for vid, rec in store.items():
        if os.path.exists(cover_path(vid)):
            rec["cover"] = 1
            have += 1
        else:
            rec.pop("cover", None)

    print(f"covers: {new} new, {skipped} cached, {len(failed)} failed | {have}/{len(store)} of catalog has one")
    for vid, why in failed:
        print(f"  COVER FAIL {vid}: {why}", flush=True)
    if failed:
        # surfaces in the Actions run summary without failing the stats write
        print(f"::warning title=Cover downloads failed::{len(failed)} of {len(videos)} covers "
              f"could not be fetched in mode={MODE}")
    return have


def scrape():
    profile_stats = None
    if MODE == "backfill":
        # Re-read an existing dataset (free) instead of running an actor - used to
        # capture covers/transcripts from a sweep that ran before those pipelines
        # existed. Reads are free; the signed asset links inside stay valid ~48h.
        ds = os.environ.get("DATASET_ID")
        if not ds:
            sys.exit("MODE=backfill needs DATASET_ID")
        url = f"https://api.apify.com/v2/datasets/{ds}/items"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = json.load(r)
        return [norm_clockworks(v) for v in raw if v.get("id")], None
    if MODE == "ig":
        tok = os.environ.get("IG_TOKEN")
        if tok:
            return scrape_ig_graph(tok)
        print("IG_TOKEN not set - falling back to the thin public scrape", flush=True)
        profs = apify("apify~instagram-profile-scraper", {"usernames": [HANDLE_IG]})
        p0 = next((p for p in profs if not p.get("error")), None)
        if p0 and p0.get("followersCount"):
            profile_stats = {
                "followers": p0["followersCount"],
                "following": p0.get("followsCount"),
                "totalVideos": p0.get("postsCount"),
            }
        raw = apify(
            "apify~instagram-scraper",
            {
                "directUrls": [f"https://www.instagram.com/{HANDLE_IG}/"],
                "resultsType": "posts",
                "resultsLimit": 50,
                "addParentData": False,
            },
        )
        videos = [norm_ig(v) for v in raw if v.get("id") and not v.get("error")]
    else:
        raw = apify(
            "clockworks~tiktok-scraper",
            {
                "profiles": [HANDLE],
                "resultsPerPage": 200 if MODE == "full" else 10,
                "shouldDownloadVideos": False,
                "shouldDownloadCovers": False,
                "shouldDownloadSubtitles": False,
            },
        )
        videos = [norm_clockworks(v) for v in raw if v.get("id")]
        # clockworks authorMeta.fans is ROUNDED (~nearest 100). Since 2026-09-06 it is
        # the only TikTok source (apidojo retired), so the follower history is rounded
        # from here on - accepted trade-off to stay on the free Apify plan.
        for item in raw:
            am = item.get("authorMeta") or {}
            if am.get("fans"):
                profile_stats = {
                    "followers": am.get("fans"),
                    "following": am.get("following"),
                    "totalVideos": am.get("video"),
                }
                break
    return videos, profile_stats


# the scrapers intermittently return an empty dataset with a clean 200 -
# retry before concluding the profile really has nothing
ATTEMPTS = 3
videos = []
profile_stats = None
for attempt in range(1, ATTEMPTS + 1):
    videos, profile_stats = scrape()
    if videos:
        break
    print(f"attempt {attempt}/{ATTEMPTS}: scrape returned 0 videos", flush=True)
    if attempt < ATTEMPTS:
        time.sleep(45)

if not videos:
    sys.exit(f"scrape returned 0 videos after {ATTEMPTS} attempts - refusing to write")

# One-time migration: Graph media ids are a different id space than the old
# scraper's, so the first Graph run starts the ig stores fresh (the scraper era
# held 8 photo posts with no counts) and drops their orphaned cover files.
if MODE == "ig" and os.environ.get("IG_TOKEN"):
    _meta = load("meta.json", {})
    if _meta.get("igSource") != "graph":
        print("first Graph API run - resetting ig stores (old scraper ids are a different id space)")
        old_ids = list(load("ig-videos.json", {}))
        for oid in old_ids:
            path = os.path.join(COVERS, f"ig-{oid}.jpg")
            if os.path.exists(path):
                os.remove(path)
        save("ig-videos.json", {})
        save("ig-timeseries.json", {})
        _meta["igSource"] = "graph"
        save("meta.json", _meta)

# merge videos (_cover/_subs are transient download urls - never persisted, they expire)
store = load(prefix + "videos.json", {})
for v in videos:
    prev = store.get(v["id"], {})
    prev.update({k: val for k, val in v.items() if not k.startswith("_")})
    prev["lastSeen"] = now_iso
    store[v["id"]] = prev

sync_covers(videos, store)
if MODE != "ig":
    sync_transcripts(videos)
save(prefix + "videos.json", store)

# per-video daily view snapshots (latest value wins within a day)
ts = load(prefix + "timeseries.json", {})
for v in videos:
    if v["views"] is not None:
        ts.setdefault(v["id"], {})[today] = v["views"]
save(prefix + "timeseries.json", ts)

# follower history
prof = load(prefix + "profile.json", {"history": [], "current": {}})
if profile_stats:
    prof["current"] = {**profile_stats, "at": now_iso}
    prof["history"].append({"at": now_iso, "followers": profile_stats["followers"]})
save(prefix + "profile.json", prof)

meta = load("meta.json", {})
meta["lastUpdated" if MODE != "ig" else "lastUpdatedIG"] = now_iso
meta["mode"] = MODE
if MODE == "full":
    meta["lastFullSweep"] = now_iso
meta["handle"] = HANDLE
save("meta.json", meta)

print(f"OK mode={MODE} videos={len(videos)} store={len(store)} followers={(profile_stats or {}).get('followers')}")
