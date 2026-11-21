#!/usr/bin/env python3
"""
Instagram viral-post scanner.

Reads accounts.txt, pulls each account's recent posts through the official
Instagram Graph API (Business Discovery), scores posts from the last 24 hours
against that account's own typical engagement, and then:
  * writes docs/data.json for the dashboard
  * sends Telegram alerts for newly flagged posts, plus a periodic digest
  * for flagged posts, reads the text at the top of the image/thumbnail (OCR),
    falls back to the caption, and builds an "X search" link for it

Usage:
  python scan.py                      # normal run (needs env vars, see README)
  python scan.py --check natgeo       # test one account, print what comes back
  python scan.py --fixture tests/fixture.json --no-telegram   # offline test

The "viral score" is a heuristic designed for this tool, not an Instagram metric.
Tune the thresholds in config.json.
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"
ARCHIVE_FILE = ROOT / "docs" / "archive.json"
THUMB_DIR = ROOT / "docs" / "thumbs"

DEFAULTS = {"scan_every_minutes": 60, "track_hours": 72, "speed_threshold": 4,
            "min_gain_per_hour": 50, "archive_days": 90, "adaptive_bars": True, "list_max": 10,
            "min_vscore_viral": 55, "min_vscore_rising": 40, "thumbs_top_live": 40}

FULL_MEDIA_FIELDS = ("id,caption,media_type,media_product_type,permalink,timestamp,"
                     "like_count,comments_count,view_count,media_url,thumbnail_url")
# Used automatically if Instagram rejects one of the optional fields above.
SAFE_MEDIA_FIELDS = "id,caption,media_type,permalink,timestamp,like_count,comments_count,media_url"

THROTTLE_CODES = {4, 17, 32, 613, 80002}
TOKEN_ERROR_CODE = 190


# ---------------------------------------------------------------- helpers
def now_utc():
    return datetime.now(timezone.utc)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_thumb(url, pid):
    """Save a small JPEG of the post so the dashboard can show it (Instagram's own image
    links expire after a while). Returns the dashboard-relative path, or None."""
    path = THUMB_DIR / f"{pid}.jpg"
    if path.exists():
        return f"thumbs/{pid}.jpg"
    if not url:
        return None
    try:
        from io import BytesIO
        from PIL import Image
        with urllib.request.urlopen(url, timeout=20) as r:
            img = Image.open(BytesIO(r.read())).convert("RGB")
        img.thumbnail((360, 450))
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        img.save(path, "JPEG", quality=72, optimize=True)
        return f"thumbs/{pid}.jpg"
    except Exception as e:
        print(f"  thumbnail failed for {pid}: {e}")
        return None


def save_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def read_accounts(path=ROOT / "accounts.txt"):
    names = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lstrip("@").strip()
        if line and line.lower() not in (n.lower() for n in names):
            names.append(line)
    return names


def parse_ts(ts):
    # Instagram format: 2026-11-20T10:00:00+0000
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")


def x_search_url(query, latest=False):
    params = {"q": query, "src": "typed_query"}
    if latest:
        params["f"] = "live"
    return "https://x.com/search?" + urllib.parse.urlencode(params)


def caption_to_query(caption, max_words):
    """First meaningful line of the caption, without hashtags/mentions/links."""
    if not caption:
        return ""
    for line in caption.splitlines():
        line = re.sub(r"https?://\S+", " ", line)
        line = re.sub(r"[#@]\w+", " ", line)
        line = re.sub(r"[^\w\s'’!?.,:-]", " ", line)  # drop emoji/symbols
        words = line.split()
        if len(words) >= 2:
            return " ".join(words[:max_words])
    return ""


# ---------------------------------------------------------------- OCR
def ocr_top_text(image_url, top_fraction, max_words):
    """Download an image and OCR the top part. Returns '' if nothing readable."""
    try:
        from io import BytesIO
        from PIL import Image, ImageOps
        import pytesseract
    except ImportError:
        print("  OCR skipped: Pillow/pytesseract not installed")
        return ""
    try:
        with urllib.request.urlopen(image_url, timeout=20) as r:
            img = Image.open(BytesIO(r.read()))
        img = img.convert("L")
        w, h = img.size
        top = img.crop((0, 0, w, max(1, int(h * top_fraction))))
        if w < 1000:  # upscale small images for better OCR
            scale = 1000 / w
            top = top.resize((int(w * scale), int(top.height * scale)))
        best = ""
        # Try normal and inverted (white meme text on dark backgrounds).
        for variant in (top, ImageOps.invert(top)):
            bw = variant.point(lambda p: 255 if p > 140 else 0)
            for candidate in (variant, bw):
                txt = pytesseract.image_to_string(candidate, config="--psm 6")
                cleaned = clean_ocr(txt)
                if score_text(cleaned) > score_text(best):
                    best = cleaned
        return " ".join(best.split()[:max_words])
    except Exception as e:  # network/image/OCR problems should never stop a scan
        print(f"  OCR failed: {e}")
        return ""


def clean_ocr(txt):
    good_lines = []
    for line in txt.splitlines():
        words = [w for w in re.findall(r"[A-Za-z0-9'’]+", line)]
        real = [w for w in words if len(w) >= 2 and re.search(r"[aeiouAEIOU]", w)]
        if len(real) >= 2 and len(real) >= 0.6 * max(1, len(words)):
            good_lines.append(" ".join(words))
    return " ".join(good_lines)


def score_text(t):
    return sum(1 for w in t.split() if len(w) >= 3)


# ---------------------------------------------------------------- Instagram API
class StopScan(Exception):
    pass


class Instagram:
    def __init__(self, cfg, token, ig_user_id, fixture=None):
        self.cfg, self.token, self.ig_user_id = cfg, token, ig_user_id
        self.fixture = fixture
        self.calls = 0
        self.fields = FULL_MEDIA_FIELDS
        self.stop_next = None

    def _get(self, params):
        url = (f"https://graph.facebook.com/{self.cfg['graph_api_version']}/{self.ig_user_id}?"
               + urllib.parse.urlencode(params))
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = json.loads(r.read())
                usage = self._usage_percent(r.headers)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except Exception:
                body = {"error": {"message": f"HTTP {e.code}", "code": -1}}
            usage = self._usage_percent(e.headers)
        self.calls += 1
        return body, usage

    @staticmethod
    def _usage_percent(headers):
        """Highest % used across Meta's rate-limit headers (0 if absent)."""
        top = 0
        for name in ("X-App-Usage", "X-Business-Use-Case-Usage"):
            raw = headers.get(name) if headers else None
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            entries = []
            if isinstance(data, dict) and "call_count" in data:
                entries = [data]
            elif isinstance(data, dict):
                for v in data.values():
                    entries.extend(v if isinstance(v, list) else [v])
            for ent in entries:
                for k in ("call_count", "total_cputime", "total_time"):
                    try:
                        top = max(top, float(ent.get(k, 0)))
                    except (TypeError, ValueError):
                        pass
        return top

    def account(self, username):
        if self.fixture is not None:
            return self.fixture_account(username)
        if self.calls >= self.cfg["max_calls_per_run"]:
            raise StopScan(f"reached max_calls_per_run ({self.cfg['max_calls_per_run']})")
        for attempt in range(2):
            fields = (f"business_discovery.username({username})"
                      f"{{username,name,followers_count,media.limit({self.cfg['posts_per_account']})"
                      f"{{{self.fields}}}}}")
            body, usage = self._get({"fields": fields, "access_token": self.token})
            time.sleep(self.cfg["seconds_between_calls"])
            if usage > self.cfg["stop_when_usage_percent_above"]:
                # Keep this result, but don't make any more calls this run.
                self.stop_next = f"rate-limit usage reached {usage:.0f}% — paused until next run"
            err = body.get("error")
            if not err:
                return body.get("business_discovery", {})
            code = err.get("code")
            if code in THROTTLE_CODES:
                raise StopScan(f"Instagram throttled the app (code {code}): {err.get('message')}")
            if code == TOKEN_ERROR_CODE:
                raise StopScan(f"ACCESS TOKEN problem (expired or revoked): {err.get('message')}")
            if code == 100 and attempt == 0 and self.fields != SAFE_MEDIA_FIELDS and "field" in err.get("message", "").lower():
                print("  an optional field was rejected; retrying with the safe field set")
                self.fields = SAFE_MEDIA_FIELDS
                continue
            return {"_error": err.get("message", "unknown error")}
        return {"_error": "failed after retry"}

    def fixture_account(self, username):
        raw = self.fixture.get(username)
        if raw is None:
            return {"_error": "Not a professional account or username not found (fixture)"}
        raw = json.loads(json.dumps(raw))
        now = now_utc()
        for m in raw.get("media", {}).get("data", []):
            if "age_hours" in m:
                m["timestamp"] = (now - timedelta(hours=m.pop("age_hours"))).strftime("%Y-%m-%dT%H:%M:%S+0000")
            if str(m.get("media_url", "")).startswith("./"):
                m["media_url"] = (ROOT / m["media_url"]).resolve().as_uri()
        return raw


# ---------------------------------------------------------------- scoring
def engagement(m, cfg):
    likes = m.get("like_count") or 0  # hidden like counts come back missing/0
    return likes + cfg["comment_weight"] * (m.get("comments_count") or 0)


def age_curve(age_h):
    """Rough share of a post's lifetime engagement reached by this age.
    Heuristic: grows with sqrt(time), ~100% at 24h, floored at 20%."""
    return max(0.2, min(1.0, (age_h / 24) ** 0.5))


def expected_rate(base, age_h):
    """Engagement per hour a *usual* post gains at this age (derivative of age_curve)."""
    return base * max(0.5 / (24 * max(age_h, 0.5)) ** 0.5, 0.01)


def viral_score(p):
    """0-100. Breakout (vs this account's own bars) matters most; real size and momentum are sanity checks.
    Weights are starting guesses, tuned on real data."""
    import math
    cl = lambda x: max(0.0, min(1.0, x))
    # Breakout is measured against this account's own bars, so accounts that often have huge
    # hits need a bigger hit to score high: rising bar -> 0.45, viral bar -> 0.75, 6x viral bar -> 1.
    rb, vb = (p.get("bars") or [2.5, 5])
    rb, vb = max(rb, 1.01), max(vb, rb * 1.01)
    x = max(p["score"], 1)
    if x <= rb:
        breakout = 0.45 * math.log(x) / math.log(rb)
    elif x <= vb:
        breakout = 0.45 + 0.30 * math.log(x / rb) / math.log(vb / rb)
    else:
        breakout = 0.75 + 0.25 * min(1.0, math.log(x / vb) / math.log(6))
    if p.get("likes_hidden"):
        real = (p.get("comments") or 0) * 15                               # rough likes-equivalent
    else:
        real = (p.get("likes") or 0) + 2 * (p.get("comments") or 0)
    size_eq = max(real, (p.get("views") or 0) / 50)
    size = cl((math.log10(max(size_eq, 1)) - 2.5) / 1.5)                   # ~300 -> 0, ~10K -> 1
    mom = cl(math.log2(max(p["speed"], 1)) / 4) if p.get("speed") is not None else breakout * 0.6
    v = 0.55 * breakout + 0.25 * size + 0.20 * mom
    if p.get("likes_hidden") and not p.get("views"):
        v *= 0.8                                                           # only comments to go on
    v *= 0.4 + 0.6 * size                                                  # tiny posts can't top the list
    return round(100 * v)


# ---------------------------------------------------------------- my pages
SUGGESTED_PAGES = {  # first-time suggestion by content type — edit with /assign or the dashboard
    "Mostlyhumour": {"ig": None, "alerts": True, "min": 55, "accounts": [
        "fboy", "memerats", "moist", "daquan", "nugget", "thehomie", "funnyposts", "memeriaz", "i_have_no_memes96_v2",
        "boysbeingboys", "rejectness", "bffreel", "cloutpapaya", "wregon", "hardestpost", "knowyourmeme", "fruitsnacks"]},
    "Penne": {"ig": None, "alerts": True, "min": 55, "accounts": [
        "fboy", "daquan", "thehomie", "boysbeingboys", "hardestpost", "i_have_no_memes96_v2", "nugget", "memerats", "moist"]},
    "Soda": {"ig": None, "alerts": True, "min": 55, "accounts": [
        "hotvocalstoday", "popdownload", "idisneys", "popstuffdaily", "didyoujustsaywig", "imstillculture"]},
    "Mostlytvmovies": {"ig": None, "alerts": True, "min": 55, "accounts": [
        "hotvocalstoday", "popdownload", "idisneys", "popstuffdaily", "didyoujustsaywig", "imstillculture"]},
}


def find_page(cfg, name):
    for k in cfg.get("my_pages", {}):
        if k.lower() == name.lower():
            return k
    return None


def pages_of(cfg, username):
    u = (username or "").lower()
    return [k for k, v in cfg.get("my_pages", {}).items() if u in [a.lower() for a in v.get("accounts", [])]]


def text_words(t):
    return set(w for w in re.findall(r"[a-z]{3,}", (t or "").lower()) if w not in {"the", "and", "you", "that", "this", "with", "for", "are"})


def same_text(a, b):
    return len(a) >= 4 and len(b) >= 4 and len(a & b) / len(a | b) >= 0.6


def pages_text(cfg):
    pg = cfg.get("my_pages", {})
    if not pg:
        return "No pages yet. Add one: /mypage Penne @penne_ig"
    out = ["<b>Your pages</b>"]
    for k, v in pg.items():
        ig = f" (@{v['ig']})" if v.get("ig") else " (no IG username — /mypage " + k + " @username to enable 'already posted')"
        al = f"alerts on, ⭐{v.get('min', 55)}+" if v.get("alerts", True) else "alerts off"
        accs = ", ".join("@" + a for a in v.get("accounts", [])) or "no tracked pages yet"
        out.append(f"\n<b>{escape(k)}</b>{escape(ig)} · {al}\n{escape(accs)}")
    return "\n".join(out)


def analyse_account(bd, cfg, now):
    """Score every post up to track_hours old. Posts within window_hours are 'live';
    older ones are only used to follow up flagged posts (24h/48h results)."""
    media = bd.get("media", {}).get("data", [])
    followers = bd.get("followers_count") or 0
    mature, tracked = [], []
    for m in media:
        if not m.get("timestamp"):
            continue
        age_h = (now - parse_ts(m["timestamp"])).total_seconds() / 3600
        m["_age_h"] = age_h
        if age_h <= cfg["track_hours"]:
            tracked.append(m)
        if age_h >= cfg["mature_after_hours"]:
            mature.append(m)

    def kind(m):
        return "REELS" if (m.get("media_product_type") == "REELS" or m.get("media_type") == "VIDEO") else "FEED"
    stats_by_kind = {}
    for k in ("REELS", "FEED"):
        sub = [m for m in mature if kind(m) == k]
        if len(sub) >= max(cfg["min_baseline_posts"], 5):
            stats_by_kind[k] = _account_stats(sub, followers, cfg)
    all_stats = _account_stats(mature, followers, cfg)
    base_eng, baseline_note, base_com, base_views, bars_likes, bars_comments = all_stats

    posts = []
    for m in tracked:
        k = kind(m)
        b_eng, _n, b_com, b_views, b_likes, b_comments = stats_by_kind.get(k, all_stats)
        posts.append(_score_post(m, bd, cfg, b_eng, b_com, b_views, b_likes, b_comments, k, k in stats_by_kind))
    summary = {"username": bd.get("username"), "name": bd.get("name"), "followers": followers,
               "baseline": round(base_eng, 1), "baseline_note": baseline_note,
               "posts_24h": sum(1 for p in posts if p["in_window"]), "error": None,
               "bars": {"likes": bars_likes, "comments": bars_comments},
               "by_type": {k: {"baseline": round(v[0], 1), "bars": v[4]} for k, v in stats_by_kind.items()}}
    return summary, posts


def _account_stats(mature, followers, cfg):
    # Many accounts hide like counts: then only comments are reliable.
    eng_vals = [engagement(m, cfg) for m in mature if m.get("like_count") is not None]
    com_vals = [m.get("comments_count") or 0 for m in mature]
    view_vals = [m["view_count"] for m in mature if m.get("view_count")]
    if len(eng_vals) >= cfg["min_baseline_posts"]:
        base_eng, baseline_note = statistics.median(eng_vals), f"median of {len(eng_vals)} older posts"
    else:
        base_eng, baseline_note = max(1, followers * 0.01), "estimated (1% of followers) — too few older posts"
    base_eng = max(base_eng, 1)
    base_com = max(statistics.median(com_vals), 1) if len(com_vals) >= cfg["min_baseline_posts"] else None
    base_views = statistics.median(view_vals) if len(view_vals) >= cfg["min_baseline_posts"] else None

    def bars(ratios):
        """Rising/viral bars: the configured levels, raised to this account's own top-25% / top-10%
        of older posts, so accounts with frequent big hits don't flag everything."""
        rb, vb = cfg["rising_threshold"], cfg["viral_threshold"]
        if cfg.get("adaptive_bars", True) and len(ratios) >= 8:
            q = statistics.quantiles(ratios, n=20)  # 5% steps
            rb, vb = max(rb, q[14]), max(vb, q[17])  # 75th / 90th percentile
        return round(rb, 2), round(max(vb, rb * 1.5), 2)
    bars_likes = bars([e / base_eng for e in eng_vals])
    bars_comments = bars([c / base_com for c in com_vals]) if base_com else bars_likes
    return base_eng, baseline_note, base_com, base_views, bars_likes, bars_comments


def _score_post(m, bd, cfg, base_eng, base_com, base_views, bars_likes, bars_comments, kind, own_kind_base):
    if True:
        curve = age_curve(m["_age_h"])
        eng = engagement(m, cfg)
        likes_hidden = m.get("like_count") is None
        if likes_hidden and base_com:
            score = (m.get("comments_count") or 0) / (base_com * curve)
            eng = round((m.get("comments_count") or 0) * base_eng / base_com)  # comparable size for min filter
        else:
            score = eng / (base_eng * curve)
        view_score = None
        if base_views and m.get("view_count"):
            view_score = m["view_count"] / (base_views * curve)
            score = max(score, view_score)
        rise_bar, viral_bar = bars_comments if (likes_hidden and base_com) else bars_likes
        if eng < cfg["min_engagement"] and not (view_score and view_score >= cfg["rising_threshold"]):
            tier = "normal"
        elif score >= viral_bar:
            tier = "viral"
        elif score >= rise_bar:
            tier = "rising"
        else:
            tier = "normal"
        return {
            "id": m["id"],
            "username": bd.get("username"),
            "permalink": m.get("permalink"),
            "type": m.get("media_product_type") or m.get("media_type"),
            "caption": (m.get("caption") or "")[:300],
            "timestamp": m.get("timestamp"),
            "age_hours": round(m["_age_h"], 1),
            "in_window": m["_age_h"] <= cfg["window_hours"],
            "likes": m.get("like_count"),
            "comments": m.get("comments_count"),
            "views": m.get("view_count"),
            "engagement": eng,
            "baseline": round(base_eng, 1),
            "score": round(score, 2),
            "view_score": round(view_score, 2) if view_score else None,
            "speed": None,
            "tier": tier,
            "bars": [rise_bar, viral_bar],
            "flag_reason": "score" if tier != "normal" else None,
            "likes_hidden": likes_hidden,
            "_image": m.get("thumbnail_url") or m.get("media_url"),
            "kind": kind,
            "kind_baseline": own_kind_base,
        }


# ---------------------------------------------------------------- Telegram
def telegram(text, token, chat_id):
    if not text:
        return
    # Telegram caps messages at 4096 chars: split on blank lines.
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if cur and len(cur) + len(block) + 2 > 3900:
            chunks.append(cur)
            cur = block
        else:
            cur = f"{cur}\n\n{block}" if cur else block
    chunks.append(cur)
    for chunk in chunks:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk[:4096], "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
        except Exception as e:
            print(f"  Telegram send failed: {e}")


def fmt_num(n):
    if n is None:
        return "–"
    return f"{n/1_000_000:.1f}M" if n >= 1_000_000 else f"{n/1000:.1f}K" if n >= 10_000 else str(n)


def post_block(p, rank_no=None):
    icon = {"viral": "🔥", "rising": "📈"}.get(p["tier"], "▫️")
    prefix = f"<b>#{rank_no}</b> " if rank_no else ""
    lines = [f"{prefix}{icon} <b>{p['tier'].upper()}</b> · @{escape(p['username'] or '')} · {p['age_hours']}h old",
             f"⭐ Viral score {p.get('vscore', '–')}/100 · {p['score']}× usual · ❤️ {'hidden' if p.get('likes_hidden') else fmt_num(p['likes'])}"
             f" · 💬 {fmt_num(p['comments'])}"
             + (f" · ▶️ {fmt_num(p['views'])}" if p.get("views") else "")]
    if p.get("speed") is not None and p.get("gain_per_hour"):
        lines.append(f"🚀 Gaining {fmt_num(p['gain_per_hour'])}/h — {p['speed']}× a usual post's pace"
                     + (" (flagged for speed)" if p.get("flag_reason") == "speed" else ""))
    if p.get("search_query"):
        lines.append(f"Text ({p['search_source']}): <i>{escape(p['search_query'])}</i>")
    links = []
    if p.get("permalink"):
        links.append(f"<a href=\"{escape(p['permalink'])}\">Open post</a>")
    if p.get("x_search_url"):
        links.append(f"<a href=\"{escape(p['x_search_url'])}\">Search on X</a>")
        links.append(f"<a href=\"{escape(p['x_search_latest_url'])}\">X latest</a>")
    lines.append(" · ".join(links))
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram commands
ACCOUNTS_HEADER = ("# Instagram accounts to watch — one per line. Easiest: use the Telegram bot\n"
                   "# (/add name, /remove name). Only Business/Creator accounts can be read.\n")
HELP_TEXT = (
    "<b>IG Viral Radar — commands</b>\n"
    "/add name1 name2 — watch accounts\n"
    "/remove name — stop watching\n"
    "/list — watched accounts\n"
    "/viral 5 — viral = 5× the account's usual engagement\n"
    "/rising 2.5 — rising = 2.5× usual\n"
    "/every 30 — scan every 30 min (15, 30, 45, 60, 120…)\n"
    "/digest 3 — extra summary every 3 hours · /digest off\n"
    "/alerts rising — alert on rising+viral · /alerts viral · /alerts off\n"
    "/minscore 40 — lowest ⭐ Viral Score (0-100) that can be flagged\n"
    "/minlikes 100 — ignore posts below 100 likes+2×comments\n"
    "/top 10 — current top posts, ranked\n"
    "/leaders — account leaderboard (7 days) · /leaders day\n"
    "/history — flagged posts & their results (week) · /history day | month | viral | fizzled\n"
    "/scan — scan at the next check\n"
    "\n<b>My pages</b>\n"
    "/pages — your pages and the accounts feeding each\n"
    "/mypage Penne @penne_ig — add a page (IG username enables the 'already posted' check)\n"
    "/assign fboy Penne Mostlyhumour — feed an account into pages · /unassign fboy Penne\n"
    "/pagealert Penne 60 — alerts for that page at ⭐60+ · /pagealert Penne off\n"
    "/delpage Penne — remove a page\n"
    "/settings — show everything\n\n"
    "Replies usually arrive within a minute or two.")


def write_accounts(names, path=ROOT / "accounts.txt"):
    Path(path).write_text(ACCOUNTS_HEADER + "".join(n + "\n" for n in names), encoding="utf-8")


def settings_text(cfg, accounts):
    return ("<b>Settings</b>\n"
            f"Accounts ({len(accounts)}): " + (", ".join("@" + a for a in accounts) or "none") + "\n"
            f"Scan every: {cfg['scan_every_minutes']} min\n"
            f"Rising: ≥ {cfg['rising_threshold']}× usual · Viral: ≥ {cfg['viral_threshold']}× usual\n"
            f"Viral Score needed: ⭐ {cfg.get('min_vscore_rising', 40)} rising · ⭐ {cfg.get('min_vscore_viral', 55)} viral\n"
            f"Ignore posts under: {cfg['min_engagement']} engagements\n"
            f"Alerts: {cfg['alert_on'] if cfg.get('alerts_enabled', True) else 'off'}\n"
            f"Digest: " + (f"every {cfg['digest_every_hours']} h" if cfg.get('digest_enabled', True) else "off"))


def tg_get_updates(token, offset):
    url = f"https://api.telegram.org/bot{token}/getUpdates?" + urllib.parse.urlencode(
        {"offset": offset, "timeout": 0, "allowed_updates": json.dumps(["message"])})
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.loads(r.read()).get("result", [])
    except Exception as e:
        print(f"  Telegram getUpdates failed: {e}")
        return []


def top_text(data, n):
    posts = (data or {}).get("posts") or []
    if not posts:
        return "No scan results yet. Try again after the next scan."
    when = data.get("generated_at", "")[:16].replace("T", " ")
    return (f"🏆 <b>Top {min(n, len(posts))} posts</b> (last {data.get('window_hours', 24)}h, scanned {when} UTC)\n\n"
            + "\n\n".join(post_block(p, i) for i, p in enumerate(posts[:n], 1)))


def leaders_rows(board, days, now=None):
    now = now or now_utc()
    cut = now - timedelta(days=days)
    stats = {}
    for v in (board or {}).values():
        if parse_ts(v["ts"]) < cut:
            continue
        s = stats.setdefault(v["u"], {"posts": 0, "viral": 0, "rising": 0, "sum": 0.0, "best": 0.0})
        s["posts"] += 1
        s["sum"] += v["best"]
        s["best"] = max(s["best"], v["best"])
        if v["tier"] in ("viral", "rising"):
            s[v["tier"]] += 1
    rows = [{"username": u, "viral": s["viral"], "rising": s["rising"], "avg": round(s["sum"] / s["posts"], 2),
             "best": round(s["best"], 2), "posts": s["posts"]} for u, s in stats.items()]
    rows.sort(key=lambda r: (r["viral"], r["rising"], r["avg"]), reverse=True)
    return rows


def leaders_text(board, days, now=None):
    rows = leaders_rows(board, days, now)
    if not rows:
        return f"No posts tracked in the last {days} day(s) yet — the leaderboard fills up as scans run."
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"🏅 <b>Account leaderboard</b> — last {days} day(s)\n"]
    for i, r in enumerate(rows[:20], 1):
        lines.append(f"{medals.get(i, f'#{i}')} @{escape(r['username'])} — 🔥 {r['viral']} viral · 📈 {r['rising']} rising · "
                     f"avg {r['avg']:.1f}× · best {r['best']:.1f}× · {r['posts']} post{'s' if r['posts'] != 1 else ''}")
    return "\n".join(lines)


def apply_command(text, cfg, accounts, ctx=None):
    """Returns (reply, config_changed, accounts_changed, scan_now)."""
    ctx = ctx or {}
    parts = text.strip().split()
    cmd = parts[0].split("@")[0].lower().lstrip("/")
    args = [a.lstrip("@").strip(",").lower() for a in parts[1:]]

    def num(lo, hi):
        try:
            v = float(args[0])
        except (IndexError, ValueError):
            return None
        return v if lo <= v <= hi else None

    if cmd in ("start", "help"):
        return HELP_TEXT, False, False, False
    if cmd in ("settings", "list"):
        return settings_text(cfg, accounts), False, False, False
    if cmd == "add":
        new = [a for a in args if re.fullmatch(r"[a-z0-9._]{1,30}", a) and a not in accounts]
        if not new:
            return "Usage: /add username (already-watched names are skipped)", False, False, False
        accounts.extend(new)
        return f"✅ Added {', '.join('@'+a for a in new)}. Now watching {len(accounts)}.", False, True, False
    if cmd == "remove":
        gone = [a for a in args if a in accounts]
        if not gone:
            return "Not in the list. Send /list to see names.", False, False, False
        for a in gone:
            accounts.remove(a)
        return f"🗑 Removed {', '.join('@'+a for a in gone)}. Now watching {len(accounts)}.", False, True, False
    if cmd in ("viral", "rising"):
        v = num(1.1, 100)
        if v is None:
            return f"Usage: /{cmd} 5  (a number between 1.1 and 100)", False, False, False
        cfg[f"{cmd}_threshold"] = v
        if cfg["rising_threshold"] >= cfg["viral_threshold"]:
            other = "viral" if cmd == "rising" else "rising"
            cfg[f"{other}_threshold"] = round(v * 2, 2) if cmd == "rising" else round(v / 2, 2)
            note = f" (adjusted {other} to {cfg[f'{other}_threshold']}× so rising stays below viral)"
        else:
            note = ""
        return f"✅ {cmd.title()} is now ≥ {v}× usual{note}.", True, False, False
    if cmd == "every":
        v = num(15, 1440)
        if v is None:
            return "Usage: /every 30  (minutes, 15 to 1440)", False, False, False
        v = int(round(v / 15) * 15)
        cfg["scan_every_minutes"] = v
        warn = ""
        if len(accounts) * 60 / v > 150:
            warn = "\n⚠️ That many scans × accounts may hit Instagram's limit; the scanner will pause itself if so."
        return f"✅ Scanning every {v} min.{warn}", True, False, False
    if cmd == "digest":
        if args and args[0] == "off":
            cfg["digest_enabled"] = False
            return "✅ Digest off.", True, False, False
        v = num(1, 24)
        if v is None:
            return "Usage: /digest 1  (hours) or /digest off", False, False, False
        cfg["digest_enabled"], cfg["digest_every_hours"] = True, v
        return f"✅ Digest every {v:g} h.", True, False, False
    if cmd == "alerts":
        a = args[0] if args else ""
        if a == "off":
            cfg["alerts_enabled"] = False
        elif a in ("rising", "viral"):
            cfg["alerts_enabled"], cfg["alert_on"] = True, a
        else:
            return "Usage: /alerts rising | viral | off", False, False, False
        return f"✅ Alerts: {a}.", True, False, False
    if cmd == "minscore":
        v = num(0, 95)
        if v is None:
            return "Usage: /minscore 40  (0-95: lowest Viral Score that can be flagged)", False, False, False
        cfg["min_vscore_rising"], cfg["min_vscore_viral"] = int(v), int(min(v + 15, 100))
        return (f"✅ Rising needs ⭐ {int(v)}+, viral needs ⭐ {cfg['min_vscore_viral']}+ "
                f"(plus beating the account's usual)."), True, False, False
    if cmd == "minlikes":
        v = num(0, 10_000_000)
        if v is None:
            return "Usage: /minlikes 100", False, False, False
        cfg["min_engagement"] = int(v)
        return f"✅ Ignoring posts under {int(v)} engagements.", True, False, False
    if cmd == "top":
        n = int(min(num(1, 10_000) or 5, 20))
        return top_text(ctx.get("data"), n), False, False, False
    if cmd in ("leaders", "leaderboard"):
        days = 1 if args and args[0] in ("day", "today", "1") else 7
        return leaders_text(ctx.get("board"), days), False, False, False
    if cmd == "history":
        return history_text(ctx.get("archive"), args[0] if args else "week"), False, False, False
    if cmd == "scan":
        return "🔎 Scanning now…", False, False, True
    raw = [a.lstrip("@").strip(",") for a in parts[1:]]
    pg = cfg.setdefault("my_pages", {})
    if cmd in ("pages", "mypages"):
        return pages_text(cfg), False, False, False
    if cmd == "mypage":
        if not raw:
            return "Usage: /mypage Penne @penne_instagram", False, False, False
        name = find_page(cfg, raw[0]) or raw[0]
        e = pg.setdefault(name, {"ig": None, "alerts": True, "min": 55, "accounts": []})
        if len(raw) > 1:
            e["ig"] = raw[1].lower()
        return f"✅ Page <b>{escape(name)}</b> saved" + (f" (@{escape(e['ig'])})" if e.get("ig") else "") + ".", True, False, False
    if cmd == "delpage":
        name = find_page(cfg, raw[0]) if raw else None
        if not name:
            return "Usage: /delpage Penne", False, False, False
        pg.pop(name)
        return f"🗑 Removed page {escape(name)}.", True, False, False
    if cmd in ("assign", "unassign"):
        if len(raw) < 2:
            return f"Usage: /{cmd} account Page [Page2 …]", False, False, False
        acc, names = raw[0].lower(), [find_page(cfg, n) for n in raw[1:]]
        if None in names:
            return "Unknown page. Your pages: " + ", ".join(pg) + " — add one with /mypage Name", False, False, False
        for n in names:
            lst = pg[n].setdefault("accounts", [])
            if cmd == "assign" and acc not in lst:
                lst.append(acc)
            if cmd == "unassign" and acc in lst:
                lst.remove(acc)
        return f"✅ @{escape(acc)} → " + (", ".join(pages_of(cfg, acc)) or "no page"), True, False, False
    if cmd == "pagealert":
        name = find_page(cfg, raw[0]) if raw else None
        if not name or len(raw) < 2:
            return "Usage: /pagealert Penne on | off | 60", False, False, False
        v = raw[1].lower()
        if v in ("on", "off"):
            pg[name]["alerts"] = v == "on"
        else:
            try:
                pg[name]["min"], pg[name]["alerts"] = max(0, min(100, int(float(v)))), True
            except ValueError:
                return "Usage: /pagealert Penne on | off | 60", False, False, False
        e = pg[name]
        return f"✅ {escape(name)}: alerts {'on, ⭐' + str(e['min']) + '+' if e['alerts'] else 'off'}", True, False, False
    return "Unknown command. Send /help.", False, False, False


def process_commands(cfg, state, tg_token, tg_chat):
    """Read new Telegram messages from the owner and apply them. Returns scan_now."""
    updates = []
    raw = os.environ.get("TG_COMMANDS", "").strip()
    if raw:  # passed in by the Cloudflare Worker (webhook mode)
        try:
            updates = [{"update_id": None, "message": m} for m in json.loads(raw)]
        except json.JSONDecodeError:
            print("  could not read TG_COMMANDS")
    else:
        updates = tg_get_updates(tg_token, state.get("tg_offset", 0))
    accounts = read_accounts()
    cfg_changed = acc_changed = scan_now = False
    for u in updates:
        if u["update_id"] is not None:
            state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(tg_chat):
            continue  # ignore anyone who isn't you
        text = msg.get("text", "")
        if not text.startswith("/"):
            telegram("Send /help to see commands.", tg_token, tg_chat)
            continue
        ctx = {"data": load_json(DATA_FILE, {}), "board": state.get("board", {}),
               "archive": load_json(ARCHIVE_FILE, {})}
        reply, c, a, s = apply_command(text, cfg, accounts, ctx)
        cfg_changed |= c
        acc_changed |= a
        scan_now |= s
        telegram(reply, tg_token, tg_chat)
    if cfg_changed:
        save_json(ROOT / "config.json", cfg)
    if acc_changed:
        write_accounts(accounts)
    return scan_now or acc_changed


def set_output(key, value):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"{key}={value}\n")


# ---------------------------------------------------------------- archive & report cards
def verdict(score, cfg, peak=None):
    cooling = peak is not None and peak > 0 and score < 0.5 * peak
    if score >= cfg["viral_threshold"]:
        return "viral", "🔥 Went viral" + (" — now cooling off" if cooling else "")
    if score >= cfg["rising_threshold"]:
        return "rising", "📈 Held up (rising)" + (" — cooling off" if cooling else "")
    return "fizzled", "💤 Fizzled out"


def snapshot(p):
    return {"age": p["age_hours"], "score": p["score"], "vscore": p.get("vscore"), "likes": p.get("likes"), "comments": p.get("comments"),
            "views": p.get("views"), "engagement": p["engagement"]}


def update_archive(archive, tracked, state, cfg, now):
    """Add newly flagged posts, follow archived ones, and return report cards to send."""
    posts = archive.setdefault("posts", {})
    by_id = {p["id"]: p for p in tracked}
    reports = []
    for p in tracked:
        if p["tier"] != "normal" and p["in_window"] and p["id"] not in posts:
            posts[p["id"]] = {
                "id": p["id"], "username": p["username"], "permalink": p["permalink"], "type": p["type"],
                "caption": p["caption"], "timestamp": p["timestamp"], "likes_hidden": p["likes_hidden"],
                "first_flagged_at": now.isoformat(timespec="seconds"), "first_flag_age": p["age_hours"],
                "first_tier": p["tier"], "flag_reason": p.get("flag_reason"),
                "peak_score": 0, "peak_tier": "normal", "peak_age": None,
                "points": [], "result_24": None, "result_48": None, "status": "live",
            }
    rank = {"normal": 0, "rising": 1, "viral": 2}
    for pid, e in posts.items():
        if e["status"] != "live":
            continue
        p = by_id.get(pid)
        if p:
            # full score history (from first sighting), as [age_h, score, engagement, comments, views]
            pts = []
            for h in state["posts"].get(pid, {}).get("history", []):
                age = (datetime.fromisoformat(h[0]) - parse_ts(e["timestamp"])).total_seconds() / 3600
                pts.append([round(age, 2), h[2] if len(h) > 2 else None, h[1],
                            h[3] if len(h) > 3 else None, h[4] if len(h) > 4 else None])
            e["points"] = pts[-120:]
            e["last"] = snapshot(p)
            for k in ("search_query", "search_source", "x_search_url", "x_search_latest_url"):
                if p.get(k):
                    e[k] = p[k]
            if p["score"] >= e["peak_score"]:
                e["peak_score"], e["peak_age"] = p["score"], p["age_hours"]
            e["peak_vscore"] = max(e.get("peak_vscore") or 0, p.get("vscore") or 0)
            if rank[p["tier"]] > rank.get(e["peak_tier"], 0):
                e["peak_tier"] = p["tier"]
        age_now = (now - parse_ts(e["timestamp"])).total_seconds() / 3600
        last = e.get("last") or {}
        if e["result_24"] is None and age_now >= 24 and last:
            code, label = verdict(last["score"], cfg, e["peak_score"])
            e["result_24"] = {**last, "verdict": code, "label": label, "stale": p is None}
            reports.append(("24h", e))
        if e["result_48"] is None and age_now >= 48 and last and last.get("age", 0) >= 40:
            code, label = verdict(last["score"], cfg, e["peak_score"])
            e["result_48"] = {**last, "verdict": code, "label": label.replace("Went viral", "Still viral"),
                              "stale": p is None}
            if code == "viral":
                reports.append(("48h", e))
        if age_now > cfg["track_hours"] or (p is None and age_now > 24 and e["result_24"] is not None
                                               and (e["result_48"] is not None or age_now > 50)):
            e["status"] = "done"
    # retention
    cut = now - timedelta(days=cfg["archive_days"])
    archive["posts"] = {k: v for k, v in posts.items() if parse_ts(v["timestamp"]) > cut}
    archive["updated"] = now.isoformat(timespec="seconds")
    archive["thresholds"] = {"rising": cfg["rising_threshold"], "viral": cfg["viral_threshold"]}
    return reports


def render_chart(e, cfg, path):
    """PNG of score over post age, with rising/viral lines. Returns path or None."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    pts = [pt for pt in e.get("points", []) if pt[1] is not None]
    if len(pts) < 2:
        return None
    xs, ys = [pt[0] for pt in pts], [pt[1] for pt in pts]
    fig, ax = plt.subplots(figsize=(6, 3.2), dpi=150)
    ax.axhspan(cfg["viral_threshold"], max(max(ys) * 1.15, cfg["viral_threshold"] * 1.3), color="#ff5a36", alpha=0.08)
    ax.axhline(cfg["viral_threshold"], color="#ff5a36", ls="--", lw=1, label=f"viral {cfg['viral_threshold']}×")
    ax.axhline(cfg["rising_threshold"], color="#e0a020", ls="--", lw=1, label=f"rising {cfg['rising_threshold']}×")
    ax.plot(xs, ys, color="#2557d6", lw=2, marker="o", ms=3)
    ax.axvline(24, color="#999", lw=0.8, ls=":")
    ax.text(24.3, 0.2, "24h", fontsize=7, color="#777")
    fa = e.get("first_flag_age")
    if fa is not None:
        near = min(pts, key=lambda pt: abs(pt[0] - fa))
        ax.plot([near[0]], [near[1]], marker="o", ms=7, mfc="none", mec="#ff5a36", mew=1.5)
        ax.annotate("flagged", (near[0], near[1]), textcoords="offset points", xytext=(6, -10), fontsize=7, color="#ff5a36")
    ax.set_xlabel("hours since posted")
    ax.set_ylabel("× usual")
    ax.set_xlim(0, max(26, max(xs) + 1))
    ax.set_ylim(0, max(max(ys) * 1.15, cfg["viral_threshold"] * 1.3))
    ax.set_title(f"@{e['username']}", fontsize=10, loc="left")
    ax.legend(fontsize=7, loc="upper left", frameon=False)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def telegram_photo(path, caption, token, chat_id):
    boundary = "----igradar" + str(int(time.time() * 1000))
    parts = []
    for name, value in (("chat_id", str(chat_id)), ("caption", caption[:1024]), ("parse_mode", "HTML")):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"chart.png\"\r\n"
                 f"Content-Type: image/png\r\n\r\n".encode() + Path(path).read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        urllib.request.urlopen(req, timeout=30)
        return True
    except Exception as ex:
        print(f"  Telegram photo failed: {ex}")
        return False


def report_text(kind, e):
    r = e["result_24"] if kind == "24h" else e["result_48"]
    likes = "hidden" if e.get("likes_hidden") else fmt_num(r.get("likes"))
    head = f"📋 <b>{kind} result</b> · @{escape(e['username'])}\n<b>{r['label']}</b>"
    body = (f"\nNow {r['score']}× usual · peak {e['peak_score']}× at {e['peak_age']}h"
            f"\n❤️ {likes} · 💬 {fmt_num(r.get('comments'))}" + (f" · ▶️ {fmt_num(r['views'])}" if r.get("views") else "")
            + f"\nFirst flagged {e['first_tier']} at {e['first_flag_age']}h"
            + (" (speed)" if e.get("flag_reason") == "speed" else ""))
    if r.get("stale"):
        body += f"\n<i>(last seen at {r['age']}h — dropped out of the account's latest posts)</i>"
    links = []
    if e.get("permalink"):
        links.append(f"<a href=\"{escape(e['permalink'])}\">Open post</a>")
    if e.get("x_search_url"):
        links.append(f"<a href=\"{escape(e['x_search_url'])}\">Search on X</a>")
    return head + body + ("\n" + " · ".join(links) if links else "")


def send_report(kind, e, cfg, token, chat_id):
    text = report_text(kind, e)
    path = render_chart(e, cfg, f"/tmp/chart_{e['id']}.png")
    if not (path and telegram_photo(path, text, token, chat_id)):
        telegram(text, token, chat_id)


def short_line(p, i):
    icon = {"viral": "🔥", "rising": "📈"}.get(p["tier"], "▫️")
    stats = (f"💬 {fmt_num(p.get('comments'))}" if p.get("likes_hidden") else f"❤️ {fmt_num(p.get('likes'))}")
    if p.get("views"):
        stats += f" · ▶️ {fmt_num(p['views'])}"
    speed = " 🚀" if p.get("flag_reason") == "speed" else ""
    text = f" — <i>{escape(p['search_query'][:60])}</i>" if p.get("search_query") else ""
    link = f" · <a href=\"{escape(p['permalink'])}\">open</a>" if p.get("permalink") else ""
    xl = f" · <a href=\"{escape(p['x_search_url'])}\">X</a>" if p.get("x_search_url") else ""
    if p.get("also_on"):
        xl += f" · 🔁 also on {len(p['also_on'])} other page{'s' if len(p['also_on']) > 1 else ''}"
    if p.get("kind") == "REELS":
        icon += "🎬"
    return (f"<b>#{i}</b> {icon} @{escape(p['username'] or '')} · ⭐ <b>{p.get('vscore', '–')}</b> · {p['score']}× usual · {p['age_hours']:.0f}h · "
            f"{stats}{speed}{text}{link}{xl}")


def alert_list_text(posts, cfg):
    rank = {"normal": 0, "rising": 1, "viral": 2}
    posts = sorted(posts, key=lambda p: (p.get("vscore", 0), p["score"]), reverse=True)
    n = cfg.get("list_max", 10)
    v = sum(1 for p in posts if p["tier"] == "viral")
    head = f"🔔 <b>{len(posts)} new hot post{'s' if len(posts) != 1 else ''}</b> ({v} viral, {len(posts) - v} rising)\n"
    lines = [short_line(p, i) for i, p in enumerate(posts[:n], 1)]
    if len(posts) > n:
        lines.append(f"…and {len(posts) - n} more — send /top 20 or open the dashboard.")
    return head + "\n".join(lines)


def page_alerts_text(posts, cfg):
    """One message, a section per page of yours (each with its own ⭐ minimum)."""
    pg = cfg.get("my_pages", {})
    if not pg:
        return alert_list_text(posts, cfg)
    posts = sorted(posts, key=lambda p: (len(p.get("also_on") or []) > 0, p.get("vscore", 0)), reverse=True)
    sections, used = [], set()
    for name, e in pg.items():
        if not e.get("alerts", True):
            used.update(p["id"] for p in posts if name in pages_of(cfg, p["username"]))
            continue
        mine = [p for p in posts if name in pages_of(cfg, p["username"]) and p.get("vscore", 0) >= e.get("min", 55)
                and name not in (p.get("posted_on") or [])]
        used.update(p["id"] for p in posts if name in pages_of(cfg, p["username"]))
        if mine:
            lines = [short_line(p, i) for i, p in enumerate(mine[:cfg.get("list_max", 10)], 1)]
            sections.append(f"<b>{escape(name)}</b> — {len(mine)} hot\n" + "\n".join(lines))
    rest = [p for p in posts if p["id"] not in used]
    if rest:
        sections.append("<b>Other accounts</b>\n" + "\n".join(short_line(p, i) for i, p in enumerate(rest[:5], 1)))
    if not sections:
        return None
    return f"🔔 <b>New hot posts</b>\n\n" + "\n\n".join(sections)


def render_grid(entries, cfg, path):
    """One image with a small chart per post (up to 6)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    entries = [e for e in entries if len([p for p in e.get("points", []) if p[1] is not None]) >= 2][:6]
    if not entries:
        return None
    cols = 2 if len(entries) > 1 else 1
    rows = (len(entries) + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(6 * cols / 1.4, 2.6 * rows), dpi=130, squeeze=False)
    for ax in axes.flat:
        ax.set_visible(False)
    for i, (ax, e) in enumerate(zip(axes.flat, entries), 1):
        ax.set_visible(True)
        pts = [p for p in e["points"] if p[1] is not None]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        top = max(max(ys) * 1.15, cfg["viral_threshold"] * 1.3)
        ax.axhspan(cfg["viral_threshold"], top, color="#ff5a36", alpha=0.08)
        ax.axhline(cfg["viral_threshold"], color="#ff5a36", ls="--", lw=0.8)
        ax.axhline(cfg["rising_threshold"], color="#e0a020", ls="--", lw=0.8)
        r = e.get("result_48") or e.get("result_24") or {}
        color = {"viral": "#d63b1f", "rising": "#c98a00", "fizzled": "#7d8594"}.get(r.get("verdict"), "#2557d6")
        ax.plot(xs, ys, color=color, lw=1.8)
        ax.axvline(24, color="#bbb", lw=0.6, ls=":")
        ax.set_xlim(0, max(26, max(xs) + 1))
        ax.set_ylim(0, top)
        ax.set_title(f"#{i} @{e['username']} — {r.get('label', '').split(' ', 1)[-1]}", fontsize=8, loc="left")
        ax.tick_params(labelsize=7)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return path


def send_reports(reports, cfg, token, chat_id):
    """All 24h/48h results from this scan in one message (+ one image of their graphs)."""
    lines = []
    for i, (kind, e) in enumerate(reports, 1):
        r = e["result_24"] if kind == "24h" else e["result_48"]
        link = f" · <a href=\"{escape(e['permalink'])}\">open</a>" if e.get("permalink") else ""
        lines.append(f"<b>#{i}</b> @{escape(e['username'])} · {kind}: <b>{r['label']}</b> · now {r['score']}× "
                     f"(peak {e['peak_score']}× at {e['peak_age']:.0f}h){link}")
    head = f"📋 <b>{len(reports)} result{'s' if len(reports) != 1 else ''}</b> — how flagged posts ended up\n"
    text = head + "\n".join(lines)
    path = render_grid([e for _, e in reports], cfg, "/tmp/results_grid.png")
    if path and len(text) <= 1000 and telegram_photo(path, text, token, chat_id):
        return
    if path:
        telegram_photo(path, "📋 Graphs for the results below", token, chat_id)
    telegram(text, token, chat_id)


def history_text(archive, arg, now=None):
    now = now or now_utc()
    items = list((archive or {}).get("posts", {}).values())
    days = 1 if arg in ("day", "today") else 30 if arg == "month" else 7
    items = [e for e in items if parse_ts(e["timestamp"]) > now - timedelta(days=days)]
    if arg in ("viral", "fizzled", "rising"):
        items = [e for e in items if (e.get("result_24") or {}).get("verdict") == arg]
    if not items:
        return "Nothing in the archive for that yet — it fills up as posts get flagged."
    items.sort(key=lambda e: e["timestamp"], reverse=True)
    lines = [f"🗂 <b>Flagged posts</b> — last {days} day(s){' · ' + arg if arg in ('viral','fizzled','rising') else ''}\n"]
    for e in items[:25]:
        r = e.get("result_24")
        status = r["label"] if r else "⏳ still live"
        age = (now - parse_ts(e["timestamp"])).total_seconds() / 3600
        lines.append(f"• @{escape(e['username'])} · {age:.0f}h ago · peak {e['peak_score']}× · {status}"
                     + (f" · <a href=\"{escape(e['permalink'])}\">open</a>" if e.get("permalink") else ""))
    if len(items) > 25:
        lines.append(f"…and {len(items) - 25} more on the dashboard.")
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", metavar="USERNAME", help="test one account and print the raw result")
    ap.add_argument("--fixture", help="offline test data instead of the live API")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--commands-only", action="store_true",
                    help="read Telegram commands, then report whether a scan is due")
    ap.add_argument("--force", action="store_true", help="scan even if not due")
    args = ap.parse_args()

    cfg = load_json(ROOT / "config.json", {})
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    if cfg.get("config_version", 1) < 2:  # one-time upgrade: read 50 posts per account
        cfg["posts_per_account"] = max(cfg.get("posts_per_account", 25), 50)
        cfg["config_version"] = 2
    if cfg.get("config_version", 1) < 3:  # one-time: one list per scan instead of a separate hourly digest
        cfg["digest_enabled"] = False
        cfg["config_version"] = 3
        if not args.fixture:
            save_json(ROOT / "config.json", {k: v for k, v in cfg.items() if k not in DEFAULTS or k == "scan_every_minutes"})
    if cfg.get("config_version", 1) < 4:  # one-time: your pages with suggested groupings
        cfg.setdefault("my_pages", json.loads(json.dumps(SUGGESTED_PAGES)))
        cfg["config_version"] = 4
        if not args.fixture:
            save_json(ROOT / "config.json", {k: v for k, v in cfg.items() if k not in DEFAULTS or k == "scan_every_minutes"})
    token = os.environ.get("IG_ACCESS_TOKEN", "")
    ig_user_id = os.environ.get("IG_USER_ID", "")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    use_tg = bool(tg_token and tg_chat) and not args.no_telegram

    if args.commands_only:
        state = load_json(STATE_FILE, {"posts": {}, "last_digest": None})
        scan_now = process_commands(cfg, state, tg_token, tg_chat) if use_tg else False
        last = state.get("last_scan")
        due = (scan_now or not last or
               (now_utc() - datetime.fromisoformat(last)).total_seconds() >= cfg["scan_every_minutes"] * 60 - 180)
        save_json(STATE_FILE, state)
        print(f"scan due: {due}")
        set_output("due", "true" if due else "false")
        return

    fixture = load_json(args.fixture, None) if args.fixture else None
    if fixture is None and not (token and ig_user_id):
        print("IG_ACCESS_TOKEN / IG_USER_ID not set yet — skipping the Instagram scan.")
        return
    ig = Instagram(cfg, token, ig_user_id, fixture)

    if args.check:
        print(json.dumps(ig.account(args.check.lstrip("@")), indent=2, ensure_ascii=False))
        print(f"\nOptional fields in use: {ig.fields}")
        return

    now = now_utc()
    state = load_json(STATE_FILE, {"posts": {}, "last_digest": None})
    archive = load_json(ARCHIVE_FILE, {"posts": {}})
    accounts = read_accounts()
    print(f"Scanning {len(accounts)} accounts at {now.isoformat(timespec='seconds')}")

    summaries, tracked, stop_reason = [], [], None
    for i, user in enumerate(accounts):
        print(f"- @{user}")
        try:
            if ig.stop_next:
                raise StopScan(ig.stop_next)
            bd = ig.account(user)
        except StopScan as e:
            stop_reason = e.args[0]
            for rest in accounts[i:]:
                summaries.append({"username": rest, "error": "skipped this run: " + stop_reason})
            print(f"  STOPPED: {stop_reason}")
            break
        if "_error" in bd:
            summaries.append({"username": user, "error": bd["_error"]})
            print(f"  error: {bd['_error']}")
            continue
        s, posts = analyse_account(bd, cfg, now)
        seen = state.setdefault("seen_accounts", [])
        if user.lower() not in seen:  # first scan of this account: learn quietly, don't alert
            seen.append(user.lower())
            for p in posts:
                p["_quiet"] = True
        summaries.append(s)
        tracked.extend(posts)

    rank = {"normal": 0, "rising": 1, "viral": 2}
    min_alert = rank.get(cfg.get("alert_on", "rising"), 1)
    new_alerts = []
    for p in tracked:
        st = state["posts"].setdefault(p["id"], {"first_seen": now.isoformat(), "alerted_tier": "normal",
                                                 "history": []})
        hist = st["history"]
        prev = hist[-1] if hist else None
        hist.append([now.isoformat(), p["engagement"], p["score"], p.get("comments"), p.get("views")])
        st["history"] = hist[-120:]
        st["timestamp"] = p["timestamp"]
        p["gain_per_hour"] = None
        if prev:
            hours = (now - datetime.fromisoformat(prev[0])).total_seconds() / 3600
            if hours >= 0.33:
                rate = (p["engagement"] - prev[1]) / hours
                p["gain_per_hour"] = round(rate)
                p["speed"] = round(rate / expected_rate(p["baseline"], max(p["age_hours"] - hours / 2, 0.5)), 1)
                # Speed-based detection: a normal post gaining unusually fast gets promoted to rising.
                if (p["in_window"] and p["tier"] == "normal" and p["speed"] >= cfg["speed_threshold"]
                        and rate >= cfg["min_gain_per_hour"] and p["engagement"] >= cfg["min_engagement"]):
                    p["tier"], p["flag_reason"] = "rising", "speed"
        p["vscore"] = viral_score(p)
        if p["tier"] == "viral" and p["vscore"] < cfg["min_vscore_viral"]:
            p["tier"] = "rising"
        if p["tier"] == "rising" and p["vscore"] < cfg["min_vscore_rising"]:
            p["tier"], p["flag_reason"] = "normal", None

        if (p["in_window"] and p["tier"] != "normal") or p["id"] in archive["posts"]:
            if "search_query" not in st:
                q, src = "", ""
                if cfg.get("ocr_enabled") and p.get("_image"):
                    q = ocr_top_text(p["_image"], cfg["ocr_top_fraction"], cfg["search_query_max_words"])
                    src = "on-image text" if q else ""
                if not q:
                    q, src = caption_to_query(p["caption"], cfg["search_query_max_words"]), "caption"
                st["search_query"], st["search_source"] = q, (src if q else "")
            p["search_query"], p["search_source"] = st["search_query"], st["search_source"]
            if p["search_query"]:
                p["x_search_url"] = x_search_url(p["search_query"])
                p["x_search_latest_url"] = x_search_url(p["search_query"], latest=True)
        if p["in_window"] and p["tier"] != "normal":
            if rank[p["tier"]] >= min_alert and rank[p["tier"]] > rank[st["alerted_tier"]]:
                if not p.get("_quiet"):
                    new_alerts.append(p)
                st["alerted_tier"] = p["tier"]
        p.pop("_quiet", None)

    # On-image text for the strongest live posts (cached), to spot the same meme on several accounts
    live_now = sorted([p for p in tracked if p["in_window"]], key=lambda p: p.get("vscore", 0), reverse=True)
    for i, p in enumerate(live_now):
        st = state["posts"][p["id"]]
        if "search_query" not in st:
            if cfg.get("ocr_enabled") and p.get("_image") and (i < cfg.get("text_match_top", 80) or p.get("kind") == "REELS"):
                q = ocr_top_text(p["_image"], cfg["ocr_top_fraction"], cfg["search_query_max_words"])
                st["search_query"], st["search_source"] = (q, "on-image text") if q else (
                    caption_to_query(p["caption"], cfg["search_query_max_words"]), "caption")
            else:  # not read yet: use the caption for now (not cached, so the image is read if it climbs)
                p.setdefault("search_query", caption_to_query(p["caption"], cfg["search_query_max_words"]))
                p.setdefault("search_source", "caption")
        p.setdefault("search_query", st.get("search_query", ""))
        p.setdefault("search_source", st.get("search_source", ""))
        if p["search_query"] and "x_search_url" not in p:
            p["x_search_url"] = x_search_url(p["search_query"])
            p["x_search_latest_url"] = x_search_url(p["search_query"], latest=True)
    words = {p["id"]: text_words(p.get("search_query")) for p in live_now}
    for p in live_now:
        p["also_on"] = sorted({o["username"] for o in live_now if o["username"] != p["username"]
                               and same_text(words[p["id"]], words[o["id"]])})

    # Already posted on my pages (pages with an IG username): compare on-image text of their recent posts
    own = state.setdefault("own_texts", {})
    for name, e in cfg.get("my_pages", {}).items():
        if not e.get("ig") or ig.stop_next:
            continue
        try:
            bd = ig.account(e["ig"])
        except StopScan:
            break
        if "_error" in bd:
            print(f"  my page @{e['ig']}: {bd['_error']}")
            continue
        texts = own.setdefault(e["ig"], {})
        for m in bd.get("media", {}).get("data", [])[:50]:
            if m["id"] not in texts:
                img = m.get("thumbnail_url") or m.get("media_url")
                t = ocr_top_text(img, cfg["ocr_top_fraction"], cfg["search_query_max_words"]) if (img and cfg.get("ocr_enabled")) else ""
                texts[m["id"]] = t or caption_to_query(m.get("caption") or "", cfg["search_query_max_words"])
        ids = [m["id"] for m in bd.get("media", {}).get("data", [])[:50]]
        own[e["ig"]] = {i: texts[i] for i in ids if i in texts}
        mine = [text_words(t) for t in own[e["ig"]].values()]
        for p in live_now:
            if any(same_text(words[p["id"]], w) for w in mine):
                p.setdefault("posted_on", []).append(name)

    for p in tracked:
        p["pages"] = pages_of(cfg, p["username"])

    # Archive: every flagged post, followed to 72h, with 24h / 48h results
    reports = update_archive(archive, tracked, state, cfg, now)

    live = [p for p in tracked if p["in_window"]]

    # Thumbnails: flagged/archived posts + the top live posts by Viral Score
    top_live = {p["id"] for p in sorted(live, key=lambda p: p.get("vscore", 0), reverse=True)[:cfg["thumbs_top_live"]]}
    for p in tracked:
        if p["id"] in archive["posts"] or p["id"] in top_live or (p["in_window"] and p["tier"] != "normal"):
            p["thumb"] = save_thumb(p.get("_image"), p["id"])
            if p["thumb"] and p["id"] in archive["posts"]:
                archive["posts"][p["id"]]["thumb"] = p["thumb"]
        p.pop("_image", None)
    keep = set(archive["posts"]) | {p["id"] for p in live if p.get("thumb")}
    if THUMB_DIR.exists():
        for f in THUMB_DIR.glob("*.jpg"):
            if f.stem not in keep:
                f.unlink()

    # Leaderboard memory: each post's best score/tier, kept 8 days
    board = state.setdefault("board", {})
    for p in live:
        b = board.setdefault(p["id"], {"u": p["username"], "ts": p["timestamp"], "best": 0, "tier": "normal"})
        b["best"] = max(b["best"], p["score"] if p["engagement"] >= cfg["min_engagement"] else 0)
        if rank[p["tier"]] > rank[b["tier"]]:
            b["tier"] = p["tier"]
    week_cut = now - timedelta(days=8)
    state["board"] = {k: v for k, v in board.items() if parse_ts(v["ts"]) > week_cut}

    # forget per-post scan history after track_hours (+1 day margin)
    cutoff = now - timedelta(hours=cfg["track_hours"] + 24)
    state["posts"] = {k: v for k, v in state["posts"].items()
                      if not v.get("timestamp") or parse_ts(v["timestamp"]) > cutoff}

    live.sort(key=lambda p: (p.get("vscore", 0), p["score"]), reverse=True)
    flagged = [p for p in live if p["tier"] != "normal"]

    if use_tg:
        if stop_reason and "TOKEN" in stop_reason:
            telegram(f"⚠️ IG scanner: {escape(stop_reason)}\nRenew IG_ACCESS_TOKEN (see README).", tg_token, tg_chat)
        if cfg.get("alerts_enabled", True) and new_alerts:
            telegram(page_alerts_text(new_alerts, cfg), tg_token, tg_chat)
        if reports:
            send_reports(reports, cfg, tg_token, tg_chat)
        last = state.get("last_digest")
        due = (not last) or (now - datetime.fromisoformat(last)).total_seconds() >= cfg["digest_every_hours"] * 3600 - 300
        if cfg.get("digest_enabled", True) and due and flagged:
            top = flagged[:cfg["digest_top_n"]]
            text = (f"🗞 <b>Digest</b> — {len(flagged)} hot post(s) in the last {cfg['window_hours']}h "
                    f"across {len(accounts)} accounts\n\n"
                    + "\n\n".join(post_block(p, i) for i, p in enumerate(top, 1)))
            telegram(text, tg_token, tg_chat)
            state["last_digest"] = now.isoformat()

    state["last_scan"] = now.isoformat()
    save_json(STATE_FILE, state)
    save_json(ARCHIVE_FILE, archive)
    save_json(DATA_FILE, {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_hours": cfg["window_hours"],
        "thresholds": {"rising": cfg["rising_threshold"], "viral": cfg["viral_threshold"]},
        "stop_reason": stop_reason,
        "api_calls": ig.calls,
        "accounts": summaries,
        "my_pages": cfg.get("my_pages", {}),
        "posts": live,
        "leaderboard": {"day": leaders_rows(state["board"], 1, now), "week": leaders_rows(state["board"], 7, now)},
    })
    print(f"Done: {len(live)} posts in window, {len(flagged)} flagged, {len(new_alerts)} new alerts, "
          f"{len(reports)} report cards, {ig.calls} API calls.")

if __name__ == "__main__":
    main()
