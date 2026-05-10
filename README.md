# signal

Personalized daily tech briefing. Fetches RSS feeds, scores articles with Claude, renders a self-contained HTML briefing page.

## What it does

1. Fetches up to 8 articles from each RSS source
2. Deduplicates by URL, filters articles older than 30 days
3. Sends articles in batches to Claude for relevance scoring (0–100)
4. Filters out anything below score 55
5. Renders a dark-themed HTML file with filter tabs, sort, and relevance badges
6. Publishes to GitHub Pages automatically via GitHub Actions

## Sources

| Source | Category |
|--------|----------|
| 9to5Mac | apple |
| MacRumors | apple |
| AppleInsider | apple |
| Anthropic Blog | ai |
| OpenAI Blog | ai |
| DeepMind Blog | ai |
| Hacker News | dev |
| The Verge | industry |
| Ars Technica | industry |
| VentureBeat AI | ai |
| MIT Tech Review | ai |
| TechCrunch | industry |
| Wired | industry |
| The Register | industry |

## Score tiers

| Score | Meaning |
|-------|---------|
| 85–100 | Top relevance |
| 65–84 | Useful context |
| 55–64 | Mildly interesting |
| < 55 | Filtered out |

## Setup

```bash
python3 -m venv ~/signal-env
source ~/signal-env/bin/activate
pip install anthropic feedparser

ANTHROPIC_API_KEY=sk-ant-... python3 briefing.py --open
```

## GitHub Actions

Runs daily on a cron schedule. Requires `ANTHROPIC_API_KEY` set as a repository secret. Commits `index.html`, `signal.json`, and a dated archive to the repo on each run.

## Output

- `index.html` — today's briefing (served via GitHub Pages)
- `signal.json` — scored articles as JSON
- `archive/YYYY-MM-DD.html` — daily archives
