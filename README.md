# IG Viral Radar

Watches Instagram accounts and sends Telegram alerts when a post from the last 24 hours is taking off.

## Control it from Telegram
Chat with your bot and type `/` to see the commands:

| Command | What it does |
|---|---|
| `/add name1 name2` | watch accounts |
| `/remove name` | stop watching |
| `/list` or `/settings` | show accounts and settings |
| `/viral 5` | viral = 5× the account's usual engagement |
| `/rising 2.5` | rising = 2.5× usual |
| `/every 30` | scan every 30 minutes (15 to 1440) |
| `/digest 1` · `/digest off` | digest every N hours |
| `/alerts rising` · `viral` · `off` | which posts trigger alerts |
| `/minlikes 100` | ignore posts below 100 likes + 2×comments |
| `/scan` | scan at the next check |

Commands are picked up every 15 minutes.

## How "viral" is decided
This is a heuristic, not an Instagram metric. Each account's usual engagement is the median of (likes + 2×comments) on its posts older than 48 hours. A new post is compared with what a usual post would have at the same age, and for Reels its views are compared the same way. The score is how many times above usual the post is.

For each flagged post, the scanner reads the text at the top of the image or thumbnail (OCR), falls back to the caption, and adds a "Search on X" link.

## Limits
- Only Business/Creator accounts can be read.
- Shares and saves aren't available for other people's posts.
- Hidden like counts reduce accuracy.
- The Instagram token needs renewing about every 60 days; the bot warns you when it expires.
