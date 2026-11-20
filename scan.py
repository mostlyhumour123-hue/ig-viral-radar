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


def analyse_account(bd, cfg, now):
    media = bd.get("media", {}).get("data", [])
    followers = bd.get("followers_count") or 0
    mature, recent = [], []
    for m in media:
        if not m.get("timestamp"):
            continue
        age_h = (now - parse_ts(m["timestamp"])).total_seconds() / 3600
        m["_age_h"] = age_h
        if age_h <= cfg["window_hours"]:
            recent.append(m)
        elif age_h >= cfg["mature_after_hours"]:
            mature.append(m)

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

    posts = []
    for m in recent:
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
        if eng < cfg["min_engagement"] and not (view_score and view_score >= cfg["rising_threshold"]):
            tier = "normal"
        elif score >= cfg["viral_threshold"]:
            tier = "viral"
        elif score >= cfg["rising_threshold"]:
            tier = "rising"
        else:
            tier = "normal"
        posts.append({
            "id": m["id"],
            "username": bd.get("username"),
            "permalink": m.get("permalink"),
            "type": m.get("media_product_type") or m.get("media_type"),
            "caption": (m.get("caption") or "")[:300],
            "timestamp": m.get("timestamp"),
            "age_hours": round(m["_age_h"], 1),
            "likes": m.get("like_count"),
            "comments": m.get("comments_count"),
            "views": m.get("view_count"),
            "engagement": eng,
            "baseline": round(base_eng, 1),
            "score": round(score, 2),
            "view_score": round(view_score, 2) if view_score else None,
            "tier": tier,
            "likes_hidden": likes_hidden,
            "_image": m.get("thumbnail_url") or m.get("media_url"),
        })
    summary = {"username": bd.get("username"), "name": bd.get("name"), "followers": followers,
               "baseline": round(base_eng, 1), "baseline_note": baseline_note,
               "posts_24h": len(recent), "error": None}
    return summary, posts


# ---------------------------------------------------------------- Telegram
def telegram(text, token, chat_id):
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=20)
    except Exception as e:
        print(f"  Telegram send failed: {e}")


def fmt_num(n):
    if n is None:
        return "–"
    return f"{n/1_000_000:.1f}M" if n >= 1_000_000 else f"{n/1000:.1f}K" if n >= 10_000 else str(n)


def post_block(p):
    icon = "🔥" if p["tier"] == "viral" else "📈"
    lines = [f"{icon} <b>{p['tier'].upper()}</b> · @{escape(p['username'] or '')} · {p['age_hours']}h old",
             f"Score {p['score']}× normal · ❤️ {fmt_num(p['likes'])} · 💬 {fmt_num(p['comments'])}"
             + (f" · ▶️ {fmt_num(p['views'])}" if p.get("views") else "")]
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
    "/digest 1 — digest every 1 hour · /digest off\n"
    "/alerts rising — alert on rising+viral · /alerts viral · /alerts off\n"
    "/minlikes 100 — ignore posts below 100 likes+2×comments\n"
    "/scan — scan at the next check\n"
    "/settings — show everything\n\n"
    "Commands are picked up every 15 minutes, so replies can take up to 15 min.")


def write_accounts(names, path=ROOT / "accounts.txt"):
    Path(path).write_text(ACCOUNTS_HEADER + "".join(n + "\n" for n in names), encoding="utf-8")


def settings_text(cfg, accounts):
    return ("<b>Settings</b>\n"
            f"Accounts ({len(accounts)}): " + (", ".join("@" + a for a in accounts) or "none") + "\n"
            f"Scan every: {cfg['scan_every_minutes']} min\n"
            f"Rising: ≥ {cfg['rising_threshold']}× usual · Viral: ≥ {cfg['viral_threshold']}× usual\n"
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


def apply_command(text, cfg, accounts):
    """Returns (reply, config_changed, accounts_changed, scan_now)."""
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
    if cmd == "minlikes":
        v = num(0, 10_000_000)
        if v is None:
            return "Usage: /minlikes 100", False, False, False
        cfg["min_engagement"] = int(v)
        return f"✅ Ignoring posts under {int(v)} engagements.", True, False, False
    if cmd == "scan":
        return "🔎 Scanning now…", False, False, True
    return "Unknown command. Send /help.", False, False, False


def process_commands(cfg, state, tg_token, tg_chat):
    """Read new Telegram messages from the owner and apply them. Returns scan_now."""
    updates = tg_get_updates(tg_token, state.get("tg_offset", 0))
    accounts = read_accounts()
    cfg_changed = acc_changed = scan_now = False
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(tg_chat):
            continue  # ignore anyone who isn't you
        text = msg.get("text", "")
        if not text.startswith("/"):
            telegram("Send /help to see commands.", tg_token, tg_chat)
            continue
        reply, c, a, s = apply_command(text, cfg, accounts)
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
    cfg.setdefault("scan_every_minutes", 60)
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
    accounts = read_accounts()
    print(f"Scanning {len(accounts)} accounts at {now.isoformat(timespec='seconds')}")

    summaries, all_posts, stop_reason = [], [], None
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
        summaries.append(s)
        all_posts.extend(posts)

    # Alerts, OCR and search links for flagged posts
    rank = {"normal": 0, "rising": 1, "viral": 2}
    min_alert = rank.get(cfg.get("alert_on", "rising"), 1)
    new_alerts = []
    for p in all_posts:
        st = state["posts"].setdefault(p["id"], {"first_seen": now.isoformat(), "alerted_tier": "normal",
                                                 "history": []})
        prev_eng = st["history"][-1][1] if st["history"] else None
        prev_t = datetime.fromisoformat(st["history"][-1][0]) if st["history"] else None
        st["history"] = (st["history"] + [[now.isoformat(), p["engagement"]]])[-48:]
        st["timestamp"] = p["timestamp"]
        if prev_eng is not None and prev_t and (now - prev_t).total_seconds() > 60:
            p["gain_per_hour"] = round((p["engagement"] - prev_eng) / ((now - prev_t).total_seconds() / 3600))
        else:
            p["gain_per_hour"] = None

        if p["tier"] != "normal":
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
            if rank[p["tier"]] >= min_alert and rank[p["tier"]] > rank[st["alerted_tier"]]:
                new_alerts.append(p)
                st["alerted_tier"] = p["tier"]
        p.pop("_image", None)

    # forget posts older than 3 days
    cutoff = now - timedelta(hours=72)
    state["posts"] = {k: v for k, v in state["posts"].items()
                      if not v.get("timestamp") or parse_ts(v["timestamp"]) > cutoff}

    all_posts.sort(key=lambda p: (rank[p["tier"]], p["score"]), reverse=True)
    flagged = [p for p in all_posts if p["tier"] != "normal"]

    if use_tg:
        if stop_reason and "TOKEN" in stop_reason:
            telegram(f"⚠️ IG scanner: {escape(stop_reason)}\nRenew IG_ACCESS_TOKEN (see README).", tg_token, tg_chat)
        if cfg.get("alerts_enabled", True):
            for p in new_alerts:
                telegram(post_block(p), tg_token, tg_chat)
        last = state.get("last_digest")
        due = (not last) or (now - datetime.fromisoformat(last)).total_seconds() >= cfg["digest_every_hours"] * 3600 - 300
        if cfg.get("digest_enabled", True) and due and flagged:
            top = flagged[:cfg["digest_top_n"]]
            text = (f"🗞 <b>Digest</b> — {len(flagged)} hot post(s) in the last {cfg['window_hours']}h "
                    f"across {len(accounts)} accounts\n\n" + "\n\n".join(post_block(p) for p in top))
            telegram(text, tg_token, tg_chat)
            state["last_digest"] = now.isoformat()

    state["last_scan"] = now.isoformat()
    save_json(STATE_FILE, state)
    save_json(DATA_FILE, {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_hours": cfg["window_hours"],
        "thresholds": {"rising": cfg["rising_threshold"], "viral": cfg["viral_threshold"]},
        "stop_reason": stop_reason,
        "api_calls": ig.calls,
        "accounts": summaries,
        "posts": all_posts,
    })
    print(f"Done: {len(all_posts)} posts in window, {len(flagged)} flagged, "
          f"{len(new_alerts)} new alerts, {ig.calls} API calls.")


if __name__ == "__main__":
    main()
