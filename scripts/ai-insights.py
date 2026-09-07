#!/usr/bin/env python3
"""Compute tag-performance statistics, then have Claude WRITE (never compute).

Division of labor, deliberate:
  - Python computes every number in this file: medians, spreads, variance
    decomposition, confounding, week-over-week. The model is never asked
    "what's interesting" against raw data.
  - Claude (Sonnet) receives the computed stats JSON and turns it into the
    weekly report + best/worst autopsies. The prompt forbids numbers that are
    not in the payload. If a claim can't be made from the payload, the model
    is told to say the data is insufficient.

The client question this answers: is performance driven by FORMAT (how it's
shot) or SCRIPT/TOPIC (what's said)? Three attacks, in rising rigor:
  1. per-axis medians + format x bucket cells (sample sizes always shown)
  2. variance decomposition: share of log-view spread each axis explains,
     with a format<->bucket confounding score (Cramer's V) that gates how
     strongly conclusions may be phrased
  3. near-duplicate scripts (transcript shingle similarity) shot in different
     formats = natural experiments, listed explicitly

Outputs data/ai-insights.json:
  {generatedAt, stats{...}, report{date,md}, autopsies{best,worst}, archive[...]}

Costs: stats are free; LLM text only regenerates when stale/changed.
Usage: ANTHROPIC_API_KEY=... python3 scripts/ai-insights.py [--stats-only] [--force]
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")
MODEL = "claude-sonnet-5"
AXES = ["format", "setting", "hook", "structure", "bucket", "cta"]
MIN_N = 4          # groups smaller than this are reported but flagged low-sample
MATURE_DAYS = 7    # views still accruing before this age; excluded from medians


def load(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default


def median(xs):
    """Rounded to whole views - half-view medians read badly in a client report."""
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    m = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    return int(round(m))


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(q * len(xs)))]


# ---------- stats ----------

def compute_stats(videos, tags, transcripts):
    now = datetime.now(timezone.utc)
    rows = []
    for vid, v in videos.items():
        t = tags.get(vid)
        if not t or not v.get("createTime") or v.get("views") is None:
            continue
        age = (now - datetime.fromisoformat(v["createTime"].replace("Z", "+00:00"))).days
        rows.append({"id": vid, "views": v["views"], "age": age,
                     "duration": v.get("duration"), "createTime": v["createTime"],
                     "caption": v.get("caption") or "", **{a: t[a] for a in AXES},
                     "hookline": t.get("hookline", "")})
    mature = [r for r in rows if r["age"] >= MATURE_DAYS]
    all_med = median([r["views"] for r in mature])
    top10 = {r["id"] for r in sorted(mature, key=lambda r: -r["views"])[:10]}

    def axis_table(rs, axis):
        groups = defaultdict(list)
        for r in rs:
            groups[r[axis]].append(r)
        out = {}
        for val, g in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            vs = [r["views"] for r in g]
            out[val] = {"n": len(g), "median": median(vs), "p25": pct(vs, .25),
                        "p75": pct(vs, .75), "vs_catalog": round(median(vs) / all_med, 2) if all_med else None,
                        "in_top10": sum(1 for r in g if r["id"] in top10),
                        "low_sample": len(g) < MIN_N}
        return out

    axes = {a: axis_table(mature, a) for a in AXES}

    # format x bucket cells
    cells = defaultdict(list)
    for r in mature:
        cells[(r["format"], r["bucket"])].append(r["views"])
    fxb = [{"format": f, "bucket": b, "n": len(vs), "median": median(vs)}
           for (f, b), vs in sorted(cells.items(), key=lambda kv: -len(kv[1]))]

    # variance decomposition on log views (eta^2 per axis)
    logs = [math.log10(r["views"] + 1) for r in mature]
    gmean = sum(logs) / len(logs)
    sst = sum((x - gmean) ** 2 for x in logs) or 1e-9
    eta = {}
    for a in AXES:
        groups = defaultdict(list)
        for r, lx in zip(mature, logs):
            groups[r[a]].append(lx)
        ssb = sum(len(g) * (sum(g) / len(g) - gmean) ** 2 for g in groups.values())
        eta[a] = round(ssb / sst, 3)

    # format<->bucket confounding (Cramer's V)
    fvals = sorted({r["format"] for r in mature})
    bvals = sorted({r["bucket"] for r in mature})
    obs = {(f, b): 0 for f in fvals for b in bvals}
    for r in mature:
        obs[(r["format"], r["bucket"])] += 1
    n = len(mature)
    chi2 = 0.0
    for f in fvals:
        for b in bvals:
            rf = sum(obs[(f, x)] for x in bvals)
            cb = sum(obs[(x, b)] for x in fvals)
            exp = rf * cb / n
            if exp > 0:
                chi2 += (obs[(f, b)] - exp) ** 2 / exp
    k = min(len(fvals), len(bvals)) - 1
    cramers_v = round(math.sqrt(chi2 / (n * k)), 3) if k > 0 else None

    # near-duplicate scripts (word 4-gram shingle Jaccard) -> natural experiments
    def shingles(text):
        w = text.lower().split()
        return {" ".join(w[i:i + 4]) for i in range(len(w) - 3)}
    texted = [(r, shingles(transcripts.get(r["id"], {}).get("t") or ""))
              for r in rows]
    texted = [(r, s) for r, s in texted if len(s) >= 12]
    dupes = []
    for i in range(len(texted)):
        for j in range(i + 1, len(texted)):
            (a, sa), (b, sb) = texted[i], texted[j]
            inter = len(sa & sb)
            if not inter:
                continue
            jac = inter / len(sa | sb)
            if jac >= 0.35:
                dupes.append({"jaccard": round(jac, 2),
                              "a": {"id": a["id"], "views": a["views"], "format": a["format"],
                                    "setting": a["setting"], "date": a["createTime"][:10]},
                              "b": {"id": b["id"], "views": b["views"], "format": b["format"],
                                    "setting": b["setting"], "date": b["createTime"][:10]}})
    dupes.sort(key=lambda d: -d["jaccard"])

    # duration buckets
    dur = axis_table_from(mature, lambda r: (
        "<15s" if (r["duration"] or 0) < 15 else
        "15-30s" if r["duration"] < 30 else
        "30-60s" if r["duration"] < 60 else "60s+"), all_med)

    # week over week
    def window(d0, d1):
        return [r for r in rows if d1 <= (now - datetime.fromisoformat(
            r["createTime"].replace("Z", "+00:00"))).days < d0]
    wk, pw = window(7, 0), window(14, 7)

    def wk_summary(rs):
        return {"posts": len(rs), "median_views": median([r["views"] for r in rs]),
                "buckets": {b: sum(1 for r in rs if r["bucket"] == b)
                            for b in {r["bucket"] for r in rs}},
                "formats": {f: sum(1 for r in rs if r["format"] == f)
                            for f in {r["format"] for r in rs}}}

    # Pick the week's best/worst ONCE, here, using the same >=2-day maturity rule
    # the autopsies and the dashboard card use. Letting the report prompt choose
    # its own made it name a different "weakest" than the autopsy card beside it.
    def pick(rs):
        if not rs:
            return None, None
        best = max(rs, key=lambda r: r["views"])
        settled = [r for r in rs if r["age"] >= 2] or rs
        worst = min(settled, key=lambda r: r["views"])
        brief = lambda r: {"id": r["id"], "views": r["views"], "age_days": r["age"],
                           "hookline": r["hookline"][:120], "format": r["format"],
                           "bucket": r["bucket"]}
        return brief(best), brief(worst)
    wk_best, wk_worst = pick(wk)
    too_new = [{"id": r["id"], "views": r["views"], "age_days": r["age"]}
               for r in wk if r["age"] < 2]

    return {
        "n_tagged": len(rows), "n_mature": len(mature),
        "week_best": wk_best, "week_worst": wk_worst,
        "week_too_new_to_judge": too_new,
        "catalog_median_views": all_med,
        "axes": axes, "format_x_bucket": fxb[:20],
        "variance_explained": eta, "format_bucket_confounding_cramers_v": cramers_v,
        "duration": dur,
        "repeated_scripts": dupes[:12],
        "this_week": wk_summary(wk), "prior_week": wk_summary(pw),
        "this_week_videos": [{"id": r["id"], "views": r["views"], "age_days": r["age"],
                              "format": r["format"], "bucket": r["bucket"], "hook": r["hook"],
                              "hookline": r["hookline"][:100]} for r in
                             sorted(wk, key=lambda r: -r["views"])],
    }, rows, mature


def axis_table_from(rs, keyfn, all_med):
    groups = defaultdict(list)
    for r in rs:
        groups[keyfn(r)].append(r["views"])
    return {k: {"n": len(vs), "median": median(vs),
                "vs_catalog": round(median(vs) / all_med, 2) if all_med else None,
                "low_sample": len(vs) < MIN_N}
            for k, vs in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


# ---------- LLM writing ----------

def claude(prompt, max_tokens=8000):   # roomy: the model thinks before it writes
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY not set (or run with --stats-only)")
    body = json.dumps({"model": MODEL, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                res = json.load(r)
            # the model may emit a thinking block first - take the text block, wherever it is
            text = next((b["text"] for b in res["content"] if b.get("type") == "text"), None)
            if text is None:
                raise SystemExit(
                    f"no text block in response (stop_reason={res.get('stop_reason')}, "
                    f"blocks={[b.get('type') for b in res['content']]}) - "
                    "if stop_reason is max_tokens, thinking consumed the budget; raise max_tokens")
            return text.strip()
        except urllib.error.HTTPError as e:
            if e.code in (429, 529, 500) and attempt < 2:
                time.sleep(20 * (attempt + 1))
                continue
            raise SystemExit(f"Claude API HTTP {e.code}: {e.read().decode(errors='replace')[:400]}")


REPORT_PROMPT = """You are writing this week's video report FOR GIGI, the creator, to read on her own dashboard. She is smart but she is not a numbers person. Write like a sharp friend who studied her videos and is telling her what they found.

VOICE RULES, non-negotiable:
- Talk straight TO her: "you", "your videos". Never "she", never "the creator", never "we".
- Write at a 5th grade reading level. Short sentences. Everyday words. One idea per sentence.
- NEVER use these words: median, average, variance, correlation, sample, data set, catalog, axis, metric, bucket, format-vs-script, confounded, statistically. Say the plain-English thing instead.
- NEVER print a bare statistic like 0.112, 1.94x, or n=7. Translate every one (see below).
- No emojis. No dashes used as punctuation. No hype. No filler like "let's dive in".
- Write view counts with commas: 2,472 not 2472. Round awkward numbers off, "about 1,800 views" beats "1,838 views".

HOW TO TRANSLATE THE NUMBERS (do this every single time):
- A group's "median" -> "your <thing> videos usually get around X views".
- "vs_catalog" is how that group compares to a normal video of yours. 1.94 -> "about twice as many views as your usual video". 3.52 -> "about three and a half times your usual views". 0.73 -> "about a quarter fewer views than usual". Round it off. Never write the number with an x.
- "catalog_median_views" -> "a normal video of yours gets about X views".
- "n" is how many videos are in that group -> "you have posted N of these". If n is under 4 or low_sample is true, you MUST add something like "that is only N videos though, so it could just be luck".
- "variance_explained" -> do NOT print these numbers at all. Just say which thing seems to matter most and which seems to matter least, in words.
- "in_top10" -> "N of your 10 biggest videos were this".
- Refer to a video by its opening line in quotes, like: the one that starts with "...".

THINK BEFORE YOU JUDGE:
- Before saying something does not matter, check how many videos actually tried it. If almost every video is the same (like nearly all talking-head), the honest answer is "you have not really tested this yet", NOT "this does not matter". Say that in plain words and name the thing she should try.
- If format and topic travel together (format_bucket_confounding_cramers_v above about 0.5), say plainly that she tends to film certain topics in certain ways, so it is hard to tell them apart yet, and name ONE specific combination she should try to find out.
- Never state a number that is not in the JSON below. Never do your own math.

BAD (never write like this): "Format explains almost none of the variance (0.028). The-come-up bucket shows 1.94x catalog median across n=22."
GOOD (write like this): "Your come-up videos, the ones about how you got started, usually pull about twice the views of a normal video. You have posted 22 of them, so that is a real pattern and not a fluke."

Use these exact markdown headers:
## This week
What happened. How many you posted, and whether views went up or down from last week. Then your best and your weakest. You MUST use "week_best" and "week_worst" for those two, exactly as given, and refer to each by its opening line. Do not pick your own from the video list, and never call a video in "week_too_new_to_judge" the weakest, because it has not had time to be seen yet (you may mention in passing that it is still too new to call). 3 to 5 short sentences.
## Is it how you film, or what you say?
The main question. Answer it in plain words. Say what the videos suggest so far, and be honest about what you cannot tell yet and why. If repeated_scripts has pairs, that is the same script filmed two ways, and it is your strongest evidence, so use it and give both view counts. End with one clear sentence saying where things stand right now.
## What is working
2 or 3 patterns that have enough videos behind them to trust. Say the numbers in plain words.
## Try this next week
2 or 3 specific things she could actually film. One sentence each. Each one should follow from something you said above.

Keep the whole thing under about 400 words.

STATS JSON:
"""

AUTOPSY_PROMPT = """Explain to Gigi, the creator, why ONE of her videos did well or badly. She reads this on her own dashboard.

VOICE RULES, non-negotiable:
- Talk straight TO her: "you", "your video". Never "she" or "the creator".
- 5th grade reading level. Short sentences. Everyday words.
- NEVER use the words median, average, catalog, benchmark, metric, or bucket. Never print a bare number like 1.94x.
- Compare in plain words instead: "about three times what a normal video of yours gets", "well under your usual".
- No emojis. No dashes as punctuation. No hedging filler.
- Only use numbers that appear below. Never do your own math.

WHAT TO COVER, in 3 to 5 sentences:
1. Start with the verdict: did it do well or badly, and roughly how it compares to a normal video of yours.
2. The opening line. Quote her actual words. The first 3 seconds have to stop the scroll, and seconds 3 to 6 have to make someone need to know what comes next. Say whether it did that.
3. Whether the video kept giving people a reason to stay after the opening.
4. If there was no call to action, say so and say what she could have asked for. If there was one, say whether it landed in a good spot. Asking right at the very end does not work because most people are already gone.
5. End with one specific thing to do differently next time.

VIDEO:
"""


def main():
    stats_only = "--stats-only" in sys.argv
    force = "--force" in sys.argv

    videos = load("videos.json", {})
    tags = load("ai-tags.json", {})
    transcripts = load("transcripts.json", {})
    prev = load("ai-insights.json", {})

    stats, rows, mature = compute_stats(videos, tags, transcripts)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = now_iso[:10]
    out = {"generatedAt": now_iso, "stats": stats,
           "report": prev.get("report"), "autopsies": prev.get("autopsies") or {},
           "archive": prev.get("archive") or []}
    print(f"stats: {stats['n_tagged']} tagged, {stats['n_mature']} mature, "
          f"catalog median {stats['catalog_median_views']}, "
          f"eta2={stats['variance_explained']}, confounding V={stats['format_bucket_confounding_cramers_v']}")

    if not stats_only:
        # weekly report: refresh if none, >6 days old, or forced
        last = (prev.get("report") or {}).get("date")
        if force or not last or (datetime.fromisoformat(today) - datetime.fromisoformat(last)).days > 6:
            md = claude(REPORT_PROMPT + json.dumps(stats, default=str))
            # only archive a genuinely older report - re-running --force on the same
            # day is a rewrite, not a new week, and must not stack duplicates
            if prev.get("report") and prev["report"].get("date") != today:
                out["archive"] = ([prev["report"]] + out["archive"])[:12]
            out["archive"] = [r for r in out["archive"] if r.get("date") != today]
            out["report"] = {"date": today, "md": md}
            print(f"report: regenerated ({len(md)} chars)")
        else:
            print(f"report: current ({last}), skipping")

        # autopsies: regenerate only when the best/worst video actually changes
        week = [r for r in rows if r["age"] < 7]
        if week:
            best = max(week, key=lambda r: r["views"])
            mature_wk = [r for r in week if r["age"] >= 2] or week
            worst = min(mature_wk, key=lambda r: r["views"])
            for label, r in (("best", best), ("worst", worst)):
                if not force and out["autopsies"].get(label, {}).get("id") == r["id"]:
                    print(f"autopsy[{label}]: unchanged ({r['id']})")
                    continue
                payload = {
                    "role": label, "views": r["views"], "age_days": r["age"],
                    "catalog_median_views": stats["catalog_median_views"],
                    "tags": {a: r[a] for a in AXES}, "hookline": r["hookline"],
                    "duration_seconds": r["duration"], "caption": r["caption"][:200],
                    "how_its_tags_usually_do": {
                        a: stats["axes"][a].get(r[a]) for a in ("format", "bucket", "hook")},
                    "transcript": (transcripts.get(r["id"], {}).get("t") or "(no speech)")[:2500],
                }
                text = claude(AUTOPSY_PROMPT + json.dumps(payload, default=str), max_tokens=4000)
                out["autopsies"][label] = {"id": r["id"], "date": today, "md": text}
                print(f"autopsy[{label}]: regenerated for {r['id']}")

    with open(os.path.join(DATA, "ai-insights.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("saved ai-insights.json")


if __name__ == "__main__":
    main()
