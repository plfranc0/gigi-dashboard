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
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


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

    return {
        "n_tagged": len(rows), "n_mature": len(mature),
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


REPORT_PROMPT = """You are writing this week's pattern report for Gigi's TikTok, read by Gigi (the creator) and Patrick (her marketing lead). Below is a JSON of statistics computed from her actual videos. Rules, non-negotiable:
- Every number you state must appear in the JSON. Never compute, extrapolate, or invent a figure.
- Any group with "low_sample": true or n below 4 may only be mentioned as "too few videos to judge".
- "vs_catalog" is that group's median views as a multiple of the whole catalog's median (1.0 = typical).
- If format and topic are confounded (format_bucket_confounding_cramers_v above ~0.5), say plainly that they travel together and name ONE specific format+topic combination she has not tried enough, as the thing to test.
- Check each axis's group sizes before judging it: when one value dominates an axis (say 80%+ of videos), low variance-explained means UNTESTED, not unimportant. Say "she almost always shoots X, so the data can't judge whether changing it would help" and suggest the variation to try. Never claim an axis "doesn't matter" when she hasn't varied it.
- Plain English, direct, warm but zero hype, no emojis, no em dashes, no bullet-point spam. Short paragraphs. It should read like a sharp friend who did the homework.

Structure (use these exact markdown headers):
## This week
2-4 sentences: posts vs prior week, median views movement, the standout and the dud (name them by hookline).
## Format or script?
The centerpiece. What the variance split and the format x bucket table actually support. If repeated_scripts contains pairs where the same script ran in two formats, use them as the strongest evidence and cite both view counts. End with a one-sentence verdict at the current level of evidence.
## What is working
2-3 patterns with real support (n >= 4), each tied to numbers from the JSON.
## Try next week
2-3 concrete, filmable suggestions that follow from the data above. Each one sentence.

Max ~380 words total.

STATS JSON:
"""

AUTOPSY_PROMPT = """Write a short performance autopsy of one TikTok video for the creator's dashboard. You get its transcript, tags, stats, and how it compares to her catalog. Rules:
- Only cite numbers present below. No invented figures.
- Quote the actual opening line when discussing the hook.
- Diagnose against these craft principles: the first 3 seconds must grab, seconds 3-6 must plant a curiosity seed, value must keep paying past the hook, CTAs die at the very end of a video.
- 3-5 sentences, plain English, direct, no emojis, no em dashes, no hedging filler. Verdict first sentence.

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
            if prev.get("report"):
                out["archive"] = ([prev["report"]] + out["archive"])[:12]
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
