#!/usr/bin/env python3
"""Tag every TikTok video against a FIXED taxonomy using the Claude API.

Two axes drive the format-vs-script question the client asked for:
  VISUAL axis  (format, setting)          <- classified from the cover image
  SCRIPT axis  (hook, structure, bucket)  <- classified from transcript + caption

Design rules, deliberate:
  - Fixed enums, never free text. Free-text tags sprawl and make the
    correlation math meaningless. Anything outside the enum is a hard failure.
  - The model NEVER sees view counts. Tags must be blind to performance or the
    analysis becomes circular.
  - The model writes tags; it computes nothing. All statistics happen in
    ai-insights.py, in Python.
  - Idempotent: a video tagged at the current TAXONOMY_V is never re-billed.
  - Invalid output = one retry with the validation error, then a loud skip.
    Garbage is never stored.

Buckets are Gigi's own six from the 8/3 strategy doc, plus catch-alls for
content that predates or sits outside the strategy.

Usage: ANTHROPIC_API_KEY=... python3 scripts/ai-tag.py [--limit N] [--dry]
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")
COVERS = os.path.join(ROOT, "assets", "covers")

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    sys.exit("ANTHROPIC_API_KEY not set")

MODEL = "claude-haiku-4-5-20251001"
TAXONOMY_V = 1

ENUMS = {
    "format": ["talking-head", "text-overlay", "broll-voiceover", "skit",
               "photo-carousel", "screen-recording", "unknown"],
    "setting": ["car", "home", "mirror-grwm", "outdoors", "event-social", "studio", "unknown"],
    "hook": ["taboo", "dark", "contradiction", "sequence-framing", "stakes",
             "scenario-injection", "polarizing", "pain-point", "question",
             "story-open", "direct-value", "relatable-moment", "no-spoken-hook"],
    "structure": ["3-levels", "options", "bad-good-great", "pain-point-solution",
                  "step-by-step", "dream-result", "heros-journey", "about-me",
                  "list", "rant-yap", "story", "moment-clip", "other"],
    "bucket": ["camera-roll-reframe", "receipts", "industry-truths", "girly-jobs",
               "the-come-up", "zoom-out-business", "relatable-life",
               "dating-relationships", "promo-cta", "other"],
    "cta": ["comment-keyword", "follow", "link-in-bio", "save-share", "watch-live", "none"],
}

PROMPT = """You are tagging one short-form TikTok video for a content analytics system. Assign EXACTLY one value per field from the allowed values. Output ONLY a JSON object, no prose, no markdown fences.

Fields and allowed values:
- format: {format}
  (what the video visually is; the attached image is its cover frame. talking-head = a person speaking to camera. text-overlay = a text-card/caption-driven video where on-screen text carries it. broll-voiceover = lifestyle footage with voice or music over it. photo-carousel = still photos. screen-recording = phone/computer screen.)
- setting: {setting}
  (where it appears to be filmed, from the cover. mirror-grwm = mirror selfie / get-ready-with-me. event-social = with friends, party, restaurant.)
- hook: {hook}
  (how the first ~3 seconds grab attention, judged from the transcript opening and caption. sequence-framing = "here are X things and the last one...". stakes = names what you lose/gain. scenario-injection = hyper-specific scenario instead of abstract claim. pain-point = names an insecurity directly. story-open = drops you into a story mid-moment. direct-value = states the value proposition plainly. relatable-moment = a shared everyday feeling. no-spoken-hook = no speech; the visual/text is the hook.)
- structure: {structure}
  (the body's skeleton. list = numbered/rapid-fire items. rant-yap = flowing unstructured talk. moment-clip = a single captured moment, no structure. bad-good-great / 3-levels / options / step-by-step / pain-point-solution / dream-result / heros-journey / about-me are the named script templates.)
- bucket: {bucket}
  (topic. camera-roll-reframe = "what's already in your camera roll is worth money". receipts = concrete earnings/deal numbers. industry-truths = how the UGC/brand world actually works, tips, mistakes. girly-jobs = soft-life/feminine-career values content. the-come-up = her personal before/origin story. zoom-out-business = money/self-worth/business thinking not UGC-specific. relatable-life = everyday relatable content, family, shopping, feelings. dating-relationships = dating/men/relationship content. promo-cta = mainly promoting the stream/link/product.)
- cta: {cta}
  (comment-keyword = asks viewers to comment a word. watch-live = pushes a live/stream.)
- hookline: the video's actual opening line, verbatim, max 120 chars. From the transcript if there is speech, else the caption text. Empty string if neither.

Video data:
CAPTION: {caption}
HASHTAGS: {hashtags}
DURATION_SECONDS: {duration}
HAS_SPEECH: {has_speech}
TRANSCRIPT: {transcript}"""


def load(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default


def save(name, obj):
    with open(os.path.join(DATA, name), "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def claude(messages, max_tokens=400):
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens,
                       "temperature": 0, "messages": messages}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                res = json.load(r)
            return res["content"][0]["text"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 529, 500) and attempt < 2:
                time.sleep(15 * (attempt + 1))
                continue
            raise SystemExit(f"Claude API HTTP {e.code}: {e.read().decode(errors='replace')[:400]}")
    raise SystemExit("unreachable")


def validate(obj):
    errs = []
    for field, allowed in ENUMS.items():
        if obj.get(field) not in allowed:
            errs.append(f"{field}={obj.get(field)!r} not in {allowed}")
    hl = obj.get("hookline")
    if not isinstance(hl, str) or len(hl) > 140:
        errs.append("hookline missing/too long")
    return errs


def tag_one(vid, v, transcript):
    has_speech = bool(transcript)
    prompt = PROMPT.format(
        **{k: " | ".join(vals) for k, vals in ENUMS.items()},
        caption=(v.get("caption") or "(none)")[:300],
        hashtags=",".join(v.get("hashtags") or []) or "(none)",
        duration=v.get("duration") or "?",
        has_speech=has_speech,
        transcript=(transcript or "(no speech in this video)")[:4000],
    )
    content = [{"type": "text", "text": prompt}]
    cover = os.path.join(COVERS, f"{vid}.jpg")
    if os.path.exists(cover):
        with open(cover, "rb") as f:
            b64 = base64.standard_b64encode(f.read()).decode()
        content.insert(0, {"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg", "data": b64}})
    else:
        content[0]["text"] += "\n(NO COVER IMAGE AVAILABLE - set format/setting to unknown unless the transcript makes them obvious.)"

    messages = [{"role": "user", "content": content}]
    for attempt in (1, 2):
        raw = claude(messages).strip()
        if raw.startswith("```"):
            raw = raw.strip("`").removeprefix("json").strip()
        try:
            obj = json.loads(raw)
            errs = validate(obj)
        except json.JSONDecodeError as e:
            obj, errs = None, [f"not valid JSON: {e}"]
        if not errs:
            return {k: obj[k] for k in [*ENUMS.keys(), "hookline"]}
        if attempt == 1:
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": "Invalid: " + "; ".join(errs) +
                          ". Reply with ONLY the corrected JSON object."}]
    raise ValueError("; ".join(errs))


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    dry = "--dry" in sys.argv

    videos = load("videos.json", {})
    transcripts = load("transcripts.json", {})
    tags = load("ai-tags.json", {})

    todo = [vid for vid in videos
            if tags.get(vid, {}).get("v") != TAXONOMY_V]
    already = len(videos) - len(todo)
    todo.sort(key=lambda vid: videos[vid].get("createTime") or "", reverse=True)
    if limit:
        todo = todo[:limit]
    print(f"{len(videos)} videos, {already} already tagged at v{TAXONOMY_V}, tagging {len(todo)}")
    if dry:
        return

    done = 0
    failed = []
    for vid in todo:
        t = (transcripts.get(vid) or {}).get("t")
        try:
            result = tag_one(vid, videos[vid], t)
            result["v"] = TAXONOMY_V
            tags[vid] = result
            done += 1
            if done % 10 == 0:
                save("ai-tags.json", tags)   # checkpoint so a crash loses <10 calls
                print(f"  ...{done}/{len(todo)}")
        except Exception as e:                                   # noqa: BLE001
            failed.append((vid, str(e)[:200]))
        time.sleep(0.4)

    save("ai-tags.json", tags)
    print(f"tagged {done}, failed {len(failed)} | store now {len(tags)}")
    for vid, why in failed:
        print(f"  TAG FAIL {vid}: {why}")
    if failed:
        print(f"::warning title=Tagging failures::{len(failed)} videos failed tagging")
        sys.exit(1 if done == 0 else 0)   # total failure = red run; partial = warn


if __name__ == "__main__":
    main()
