# IG Viral Radar

Watches Instagram accounts and sends Telegram alerts when a post from the last 24 hours is taking off. Flagged posts are followed for 72 hours, so you see whether they went viral, held up, or fizzled.

## Control it from Telegram
Chat with your bot and type `/` to see the commands:

| Command | What it does |
|---|---|
| `/add name1 name2` · `/remove name` | watch or stop watching accounts (triggers a scan) |
| `/scan` | scan now |
| `/top 10` | current top posts, ranked |
| `/leaders` · `/leaders day` | account leaderboard |
| `/history` · `day` · `month` · `viral` · `fizzled` | flagged posts and their results |
| `/viral 5` · `/rising 2.5` | how many × usual counts as viral / rising |
| `/every 30` | scan every 30 minutes |
| `/digest 1` · `/digest off` | digest every N hours |
| `/alerts rising` · `viral` · `off` | which posts trigger alerts |
| `/minlikes 100` | ignore posts below 100 likes + 2×comments |
| `/list` · `/settings` | show accounts and settings |

With the Cloudflare Worker connected, replies arrive within a minute or two.

## How "viral" is decided
This is a heuristic, not an Instagram metric.
- **Usual engagement:** median likes + 2×comments of the account's posts older than 48h. If likes are hidden, comments are compared with the account's usual comments.
- **Score:** how many times above a usual post *at the same age* the post is. For Reels, views are compared too.
- **Speed:** how fast a post is gaining since the last scan, compared with a usual post's pace. A post gaining more than 4× faster than usual is flagged *rising* even if its total is still low.

## What happens after 24 hours
Every rising or viral post goes into the **archive** (`docs/archive.json`, kept 90 days) and is followed until it is 72 hours old.
- **At 24h:** a report card with a graph and a verdict: 🔥 Went viral · 📈 Held up · 💤 Fizzled out (plus "cooling off" if it ended at less than half its peak).
- **At 48h:** a follow-up if it is still viral.
- **Dashboard → Flagged:** filter by Live, 24–48h, Still viral 48h+, Went viral, Held up, Fizzled; sort by recent, peak, 24h result, or hottest now.

## Instant commands (Cloudflare Worker)
`worker/worker.js` runs on Cloudflare's free plan. It receives Telegram messages instantly and starts the GitHub workflow, and its 15-minute timer keeps scans on schedule (GitHub's own schedule is best-effort and often runs late). It needs four settings: `TG_TOKEN`, `GH_TOKEN` (fine-grained, Actions read/write on this repo), `CHAT_ID`, `REPO`. Open `https://<worker>.workers.dev/setup` once to connect the bot.

## Limits
- Only Business/Creator accounts can be read.
- Shares and saves aren't available for other people's posts.
- The Instagram token needs renewing about every 60 days; the bot warns you when it expires.
- Thresholds (5× viral, 2.5× rising, 4× speed) are starting guesses; tune them after a few days.
