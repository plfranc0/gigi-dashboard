#!/usr/bin/env python3
"""One-time Instagram Graph API connection: auth code -> 60-day token, then probe.

Usage:
    IG_APP_ID_SCA=... IG_APP_SECRET_SCA=... python3 scripts/ig-connect.py <code>

The auth code Maya sends back expires 1 HOUR after she taps Allow, so this does
everything in a single run: exchanges the code, upgrades to a long-lived token,
and immediately probes what the account actually exposes. The probe matters --
metric names differ by media type and API version, so the pull is built against
what came back, never against a guess.

Nothing here is written to disk. The token is printed once; store it in .env as
IG_TOKEN_SCA and as the repo secret IG_TOKEN.

Refresh (token lasts 60 days, refreshable any time after 24h):
    GET graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=...
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

APP_ID = os.environ.get("IG_APP_ID_SCA")
APP_SECRET = os.environ.get("IG_APP_SECRET_SCA")
REDIRECT = "https://plfranc0.github.io/gigi-dashboard/auth.html"

if not APP_ID or not APP_SECRET:
    sys.exit("IG_APP_ID_SCA / IG_APP_SECRET_SCA not set")
if len(sys.argv) < 2:
    sys.exit("usage: ig-connect.py <auth code from Maya>")

# Instagram appends '#_' to the redirect; auth.html strips it, but a hand-pasted
# code may still carry it (or surrounding whitespace).
code = sys.argv[1].strip().replace("#_", "")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "gigi-dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} on {url.split('?')[0]}\n{body}")


def post(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "gigi-dashboard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} on {url}\n{body}\n\n"
                         "If this says the code is invalid or expired, ask Maya to open the\n"
                         "connection link again - a fresh code costs nothing.")


print("1/4 exchanging auth code for a short-lived token...")
short = post("https://api.instagram.com/oauth/access_token", {
    "client_id": APP_ID,
    "client_secret": APP_SECRET,
    "grant_type": "authorization_code",
    "redirect_uri": REDIRECT,
    "code": code,
})
short_token = short.get("access_token")
user_id = short.get("user_id")
if not short_token:
    sys.exit(f"no access_token in response: {short}")
print(f"    ok, user_id={user_id}, permissions={short.get('permissions')}")

print("2/4 upgrading to a 60-day long-lived token...")
long_res = get("https://graph.instagram.com/access_token?" + urllib.parse.urlencode({
    "grant_type": "ig_exchange_token",
    "client_secret": APP_SECRET,
    "access_token": short_token,
}))
token = long_res.get("access_token")
if not token:
    sys.exit(f"no long-lived token in response: {long_res}")
print(f"    ok, expires_in={long_res.get('expires_in')}s (~{int(long_res.get('expires_in',0))//86400} days)")

print("3/4 reading the account...")
me = get("https://graph.instagram.com/v23.0/me?" + urllib.parse.urlencode({
    "fields": "user_id,username,name,account_type,followers_count,follows_count,media_count",
    "access_token": token,
}))
print("    " + json.dumps(me))

print("4/4 probing media (this is what the dashboard will read)...")
media = get("https://graph.instagram.com/v23.0/me/media?" + urllib.parse.urlencode({
    "fields": ("id,caption,media_type,media_product_type,permalink,thumbnail_url,"
               "media_url,timestamp,like_count,comments_count,is_shared_to_feed"),
    "limit": 100,
    "access_token": token,
}))
items = media.get("data", [])
kinds = {}
for m in items:
    kinds[(m.get("media_type"), m.get("media_product_type"))] = \
        kinds.get((m.get("media_type"), m.get("media_product_type")), 0) + 1
print(f"    {len(items)} media on page 1, next_page={'yes' if media.get('paging',{}).get('next') else 'no'}")
print(f"    breakdown by (media_type, media_product_type): {kinds}")
if items:
    print("    sample item: " + json.dumps(items[0])[:600])

# insights shapes differ per product type, so try one of each and report honestly
for label, want in (("REELS", "REELS"), ("FEED", "FEED")):
    sample = next((m for m in items if m.get("media_product_type") == want), None)
    if not sample:
        print(f"    no {label} media found to probe insights on")
        continue
    metrics = ("views,reach,likes,comments,saved,shares,total_interactions"
               if want == "REELS" else "views,reach,likes,comments,saved,shares,total_interactions")
    try:
        ins = get(f"https://graph.instagram.com/v23.0/{sample['id']}/insights?" +
                  urllib.parse.urlencode({"metric": metrics, "access_token": token}))
        got = {d["name"]: d["values"][0]["value"] for d in ins.get("data", [])}
        print(f"    {label} insights OK -> {got}")
    except SystemExit as e:
        print(f"    {label} insights FAILED -> {e}")

print("\n" + "=" * 68)
print("LONG-LIVED TOKEN (store as IG_TOKEN_SCA in .env and repo secret IG_TOKEN):")
print(token)
print("=" * 68)
