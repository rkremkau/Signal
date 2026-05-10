#!/usr/bin/env python3
"""
briefing.py — personalized tech briefing generator
Fetches RSS feeds, scores with Claude, writes index.html, optionally posts to Slack.

Usage:
  pip install anthropic feedparser
  ANTHROPIC_API_KEY=sk-ant-... python3 briefing.py --output index.html --open
  ANTHROPIC_API_KEY=sk-ant-... SLACK_WEBHOOK_URL=https://... python3 briefing.py --slack
"""

import argparse
import calendar
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import feedparser

# ── PROFILE ───────────────────────────────────────────────────────────────────

PROFILE = """
I work at Apple leading a consulting engineering team focused on enterprise
customers globally. I sit between field engineering, customers/partners, and
internal product/software/hardware/devrel/marketing teams.

Core focus areas:
- Apple platform capabilities (macOS, iOS, visionOS, Apple silicon)
- AI/LLM: on-device inference, edge AI, MLX framework, MCP protocol,
  agentic workflows, model fine-tuning, automation pipelines
- Enterprise AI adoption — how customers deploy AI at scale
- Developer tools: Claude API, Cursor, Xcode, Swift, automation
- Apple enterprise: MDM, Jamf, deployment, security, Apple @ Work

I'm actively building with the Claude API, learning model training,
and want to stay sharp for customer conversations and internal teams.
"""

HOBBY_CONTEXT = """
Lower priority but include if genuinely interesting:
SFF PC builds, HiFi audio (KEF, Focal, SVS), gaming hardware,
EV/car tech, motorcycle electronics, World of Warcraft patches.
"""

# ── SOURCES ───────────────────────────────────────────────────────────────────

SOURCES = [
    {"name": "9to5Mac",         "tier": 1, "cat": "apple",    "url": "https://9to5mac.com/feed/"},
    {"name": "MacRumors",       "tier": 1, "cat": "apple",    "url": "https://feeds.macrumors.com/MacRumors-All"},
    {"name": "AppleInsider",    "tier": 1, "cat": "apple",    "url": "https://appleinsider.com/rss/news/"},
    {"name": "Anthropic Blog",  "tier": 1, "cat": "ai",       "url": "https://www.anthropic.com/rss.xml"},
    {"name": "OpenAI Blog",     "tier": 1, "cat": "ai",       "url": "https://openai.com/blog/rss.xml"},
    {"name": "DeepMind Blog",   "tier": 1, "cat": "ai",       "url": "https://deepmind.google/blog/rss.xml"},
    {"name": "Hacker News",     "tier": 1, "cat": "dev",      "url": "https://hnrss.org/frontpage"},
    {"name": "The Verge",       "tier": 2, "cat": "industry", "url": "https://www.theverge.com/rss/index.xml"},
    {"name": "Ars Technica",    "tier": 2, "cat": "industry", "url": "http://feeds.arstechnica.com/arstechnica/index"},
    {"name": "VentureBeat AI",  "tier": 2, "cat": "ai",       "url": "https://venturebeat.com/category/ai/feed/"},
    {"name": "TechCrunch",      "tier": 3, "cat": "industry", "url": "https://techcrunch.com/feed/"},
    {"name": "Wired",           "tier": 3, "cat": "industry", "url": "https://www.wired.com/feed/rss"},
    {"name": "The Register",    "tier": 3, "cat": "industry", "url": "https://www.theregister.com/headlines.atom"},
    {"name": "MIT Tech Review", "tier": 2, "cat": "ai",       "url": "https://www.technologyreview.com/feed/"},
]

ITEMS_PER_FEED = 8
MIN_SCORE      = 55
SCORE_BATCH    = 30
MODEL          = "claude-haiku-4-5-20251001"
MAX_AGE_DAYS   = 30

# ── FETCH ─────────────────────────────────────────────────────────────────────

def fetch_feeds():
    articles = []
    for src in SOURCES:
        print(f"  fetching {src['name']}...", end=" ", flush=True)
        try:
            feed = feedparser.parse(src["url"])
            count = 0
            for entry in feed.entries[:ITEMS_PER_FEED]:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "")
                summary = ""
                for field in ("summary", "description", "content"):
                    val = entry.get(field, "")
                    if isinstance(val, list):
                        val = val[0].get("value", "") if val else ""
                    if val:
                        summary = re.sub(r"<[^>]+>", " ", val).strip()[:400]
                        break
                if not title:
                    continue
                pub = ""
                pub_ts = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        pub_ts = calendar.timegm(entry.published_parsed)
                        pub = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()
                    except Exception:
                        pass
                if pub_ts:
                    age_days = (time.time() - pub_ts) / 86400
                    if age_days > MAX_AGE_DAYS:
                        continue
                articles.append({
                    "title":     title,
                    "url":       link,
                    "source":    src["name"],
                    "tier":      src["tier"],
                    "cat":       src["cat"],
                    "summary":   summary,
                    "pubDate":   pub,
                    "score":     None,
                    "aiSummary": "",
                    "tags":      [],
                    "why":       "",
                })
                count += 1
            print(f"✓ {count} articles")
        except Exception as e:
            print(f"✗ {e}")

    seen = set()
    deduped = []
    for a in articles:
        key = a["url"].lower()[:100]
        if key and key not in seen:
            seen.add(key)
            deduped.append(a)
    return deduped

# ── SCORE ─────────────────────────────────────────────────────────────────────

def score_batch(client, articles):
    lines = "\n".join(
        f'{i}: [{a["source"]}] {a["title"]} — {a["summary"][:120]}'
        for i, a in enumerate(articles)
    )
    prompt = f"""You are a relevance engine for this specific person.

USER PROFILE:
{PROFILE.strip()}

HOBBY TECH (much lower priority, max score 60):
{HOBBY_CONTEXT.strip()}

SCORING:
85-100 = directly relevant to their Apple CE role, AI/LLM learning, enterprise tech, or dev tools
65-84  = useful context — platform news, tech industry signals, adjacent AI topics
50-64  = mildly interesting — hobby crossover, general tech
below 50 = omit

ARTICLES TO SCORE (index: [source] title — snippet):
{lines}

For each article scoring 50+, return a JSON object:
- idx: the index number (integer)
- score: 0-100 (integer)
- category: one of apple | ai | enterprise | dev | industry | hobby
- summary: one sentence max 20 words — why it matters to THIS user specifically
- tags: array of 1-3 short keyword tags
- why: 5-7 word reason for the score

Return ONLY a valid JSON array. No markdown, no explanation."""

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text if msg.content else ""
        s = text.find("[")
        e = text.rfind("]")
        if s == -1 or e == -1:
            return []
        return json.loads(text[s:e+1])
    except Exception as ex:
        print(f"  scoring error: {ex}")
        return []

def score_articles(client, articles):
    scored = []
    for i in range(0, len(articles), SCORE_BATCH):
        batch = articles[i:i + SCORE_BATCH]
        print(f"  scoring articles {i+1}–{min(i+SCORE_BATCH, len(articles))} of {len(articles)}...", flush=True)
        results = score_batch(client, batch)
        for r in results:
            idx = r.get("idx")
            if idx is None or not isinstance(idx, int) or idx >= len(batch):
                continue
            sc = int(r.get("score", 0))
            if sc < MIN_SCORE:
                continue
            a = batch[idx].copy()
            a["score"]     = sc
            a["cat"]       = r.get("category", a["cat"])
            a["aiSummary"] = r.get("summary", "")
            a["tags"]      = r.get("tags", [])
            a["why"]       = r.get("why", "")
            scored.append(a)
        time.sleep(0.3)
    return scored

# ── SLACK ─────────────────────────────────────────────────────────────────────

def post_slack(articles, webhook_url, pages_url=""):
    top = [a for a in articles if a["score"] >= 85][:10]
    mid = [a for a in articles if 65 <= a["score"] < 85][:5]
    show = top + mid

    date_str = datetime.now().strftime("%B %d, %Y")
    top_count = len([a for a in articles if a["score"] >= 85])

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"// signal — {date_str}"}
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"{len(articles)} articles · {top_count} top relevance"}]
        },
        {"type": "divider"},
    ]

    for a in show:
        score = a["score"]
        emoji = "🟣" if score >= 85 else "🟢" if score >= 65 else "🟡"
        title = a["title"]
        url   = a.get("url", "")
        title_md = f"<{url}|{title}>" if url else title
        why   = a.get("why", "")
        src   = a.get("source", "")
        tags  = " ".join(f"`{t}`" for t in a.get("tags", []))
        summary = a.get("aiSummary") or ""

        text = f"{emoji} *{score}* {title_md}\n{summary}"
        if why or src or tags:
            meta = " · ".join(filter(None, [src, tags, f"_{why}_" if why else ""]))
            text += f"\n{meta}"

        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})

    if pages_url:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"<{pages_url}|View full briefing →>"}]
        })

    payload = json.dumps({"blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"  ✓ Slack posted ({resp.status})")
    except Exception as e:
        print(f"  ✗ Slack error: {e}")

# ── RENDER HTML ───────────────────────────────────────────────────────────────

def render_html(articles, output_path):
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    articles_json = json.dumps(articles, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>signal — {datetime.now().strftime("%b %d")}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@400;500;600&display=swap');
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg: #0a0a0f; --surface: #12121a; --surface2: #1a1a26;
    --border: rgba(255,255,255,0.07); --border2: rgba(255,255,255,0.13);
    --text: #e8e8f0; --muted: #6868a0;
    --accent: #6e6aff; --accent2: #a78bfa;
    --green: #34d399; --amber: #fbbf24;
    --mono: 'DM Mono', monospace; --sans: 'DM Sans', sans-serif;
  }}
  body {{ font-family: var(--sans); background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.6; min-height: 100vh; }}
  .app {{ max-width: 900px; margin: 0 auto; padding: 28px 20px; }}
  .header {{ display: flex; align-items: center; gap: 14px; margin-bottom: 26px; padding-bottom: 18px; border-bottom: 1px solid var(--border); }}
  .logo {{ font-family: var(--mono); font-size: 16px; color: var(--accent2); }}
  .header-meta {{ font-family: var(--mono); font-size: 11px; color: var(--muted); }}
  .header-right {{ margin-left: auto; font-family: var(--mono); font-size: 11px; color: var(--muted); }}
  .filters {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 18px; }}
  .fp {{ font-family: var(--mono); font-size: 11px; padding: 3px 11px; border-radius: 20px; border: 1px solid var(--border2); color: var(--muted); cursor: pointer; transition: all 0.15s; background: transparent; }}
  .fp.active, .fp:hover {{ border-color: var(--accent); color: var(--accent2); background: rgba(110,106,255,0.08); }}
  .sort-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }}
  .sl {{ font-family: var(--mono); font-size: 11px; color: var(--muted); }}
  .sb {{ background: transparent; border: 1px solid var(--border2); color: var(--muted); padding: 3px 10px; border-radius: 5px; font-family: var(--mono); font-size: 11px; cursor: pointer; transition: all 0.15s; }}
  .sb.active {{ border-color: var(--accent); color: var(--accent2); }}
  .cb {{ margin-left: auto; font-family: var(--mono); font-size: 11px; color: var(--muted); }}
  .feed {{ display: flex; flex-direction: column; gap: 10px; }}
  .fi {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 15px 17px; transition: border-color 0.15s; }}
  .fi:hover {{ border-color: var(--border2); }}
  .it {{ display: flex; align-items: flex-start; gap: 10px; margin-bottom: 7px; }}
  .sp {{ font-family: var(--mono); font-size: 11px; font-weight: 500; padding: 2px 7px; border-radius: 4px; flex-shrink: 0; margin-top: 2px; }}
  .shi {{ background: rgba(110,106,255,0.15); color: var(--accent2); border: 1px solid rgba(110,106,255,0.3); }}
  .smd {{ background: rgba(52,211,153,0.1); color: var(--green); border: 1px solid rgba(52,211,153,0.25); }}
  .slo {{ background: rgba(251,191,36,0.1); color: var(--amber); border: 1px solid rgba(251,191,36,0.2); }}
  .tti {{ font-size: 14px; font-weight: 500; color: var(--text); line-height: 1.4; flex: 1; }}
  .tti a {{ color: inherit; text-decoration: none; }}
  .tti a:hover {{ color: var(--accent2); }}
  .sm {{ font-size: 12px; color: var(--muted); line-height: 1.6; margin-bottom: 9px; }}
  .foot {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
  .src {{ font-family: var(--mono); font-size: 10px; color: var(--muted); padding: 2px 6px; border-radius: 3px; background: var(--surface2); }}
  .tag {{ font-family: var(--mono); font-size: 10px; color: var(--accent); padding: 2px 6px; border-radius: 3px; background: rgba(110,106,255,0.08); }}
  .why {{ font-family: var(--mono); font-size: 10px; color: var(--muted); font-style: italic; margin-left: auto; text-align: right; max-width: 200px; }}
  .stats {{ font-family: var(--mono); font-size: 11px; color: var(--muted); margin-bottom: 18px; padding: 10px 14px; background: var(--surface); border-radius: 8px; border: 1px solid var(--border); display: flex; gap: 20px; flex-wrap: wrap; }}
  .stats span {{ color: var(--text); }}
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <div>
      <div class="logo">// signal</div>
      <div class="header-meta">personalized tech briefing</div>
    </div>
    <div class="header-right">{now}</div>
  </div>
  <div class="stats" id="stats"></div>
  <div class="filters">
    <span class="fp active" onclick="filterCat('all',this)">all</span>
    <span class="fp" onclick="filterCat('apple',this)">apple</span>
    <span class="fp" onclick="filterCat('ai',this)">ai / llm</span>
    <span class="fp" onclick="filterCat('enterprise',this)">enterprise</span>
    <span class="fp" onclick="filterCat('dev',this)">dev tools</span>
    <span class="fp" onclick="filterCat('industry',this)">industry</span>
    <span class="fp" onclick="filterCat('hobby',this)">hobby</span>
  </div>
  <div class="sort-row">
    <span class="sl">sort:</span>
    <button class="sb active" onclick="sortBy('score',this)">relevance</button>
    <button class="sb" onclick="sortBy('time',this)">newest</button>
    <span class="cb" id="cb"></span>
  </div>
  <div class="feed" id="feed"></div>
</div>
<script>
const ALL = {articles_json};
let currentFilter = 'all';
let currentSort = 'score';
function esc(s) {{ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }}
function renderStats() {{
  const cats = {{}};
  ALL.forEach(a => {{ cats[a.cat] = (cats[a.cat]||0)+1; }});
  const top = ALL.filter(a=>a.score>=85).length;
  document.getElementById('stats').innerHTML =
    `<div>total <span>${{ALL.length}}</span></div>` +
    `<div>top relevance <span>${{top}}</span></div>` +
    Object.entries(cats).map(([k,v])=>`<div>${{k}} <span>${{v}}</span></div>`).join('');
}}
function render() {{
  let items = [...ALL];
  if (currentFilter !== 'all') items = items.filter(i=>i.cat===currentFilter);
  if (currentSort === 'score') items.sort((a,b)=>(b.score||0)-(a.score||0));
  else items.sort((a,b)=>new Date(b.pubDate||0)-new Date(a.pubDate||0));
  document.getElementById('cb').textContent = items.length + ' articles';
  document.getElementById('feed').innerHTML = items.map(it => {{
    const sc = it.score>=85?'shi':it.score>=65?'smd':'slo';
    const tags = (it.tags||[]).map(t=>`<span class="tag">${{esc(t)}}</span>`).join('');
    const title = it.url ? `<a href="${{esc(it.url)}}" target="_blank" rel="noopener">${{esc(it.title)}}</a>` : esc(it.title);
    return `<div class="fi">
      <div class="it"><span class="sp ${{sc}}">${{it.score}}</span><div class="tti">${{title}}</div></div>
      <div class="sm">${{esc(it.aiSummary||it.summary||'')}}</div>
      <div class="foot"><span class="src">${{esc(it.source)}}</span>${{tags}}${{it.why?`<span class="why">${{esc(it.why)}}</span>`:''}}</div>
    </div>`;
  }}).join('');
}}
function filterCat(cat, el) {{ currentFilter=cat; document.querySelectorAll('.fp').forEach(p=>p.classList.remove('active')); el.classList.add('active'); render(); }}
function sortBy(mode, el) {{ currentSort=mode; document.querySelectorAll('.sb').forEach(b=>b.classList.remove('active')); el.classList.add('active'); render(); }}
renderStats(); render();
</script>
</body>
</html>"""

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"\n  ✓ written to {output_path}")

def render_json(articles, output_path):
    data = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "count": len(articles),
        "articles": articles,
    }
    Path(output_path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"  ✓ json written to {output_path}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="signal — personalized tech briefing")
    parser.add_argument("--key", help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    parser.add_argument("--output", default="index.html", help="Output HTML file")
    parser.add_argument("--open", action="store_true", help="Open in browser when done")
    parser.add_argument("--slack", action="store_true", help="Post top articles to Slack")
    parser.add_argument("--slack-url", help="Slack webhook URL (or set SLACK_WEBHOOK_URL env var)")
    parser.add_argument("--pages-url", default="", help="GitHub Pages URL to include in Slack message")
    args = parser.parse_args()

    api_key = args.key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: set ANTHROPIC_API_KEY or use --key sk-ant-...")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    print("── signal ─────────────────────────────────")
    print(f"  {datetime.now().strftime('%B %d, %Y %I:%M %p')}")
    print("────────────────────────────────────────────")

    print("\n[1/3] fetching feeds...")
    articles = fetch_feeds()
    print(f"  → {len(articles)} articles fetched")

    print("\n[2/3] scoring with Claude...")
    scored = score_articles(client, articles)
    scored.sort(key=lambda a: a["score"], reverse=True)
    print(f"  → {len(scored)} articles above threshold ({MIN_SCORE})")

    print("\n[3/3] rendering output...")
    render_html(scored, args.output)
    json_path = str(args.output).replace(".html", ".json") if args.output.endswith(".html") else args.output + ".json"
    render_json(scored, json_path)

    if args.open:
        import subprocess
        subprocess.run(["open", args.output])
        print("  ✓ opened in browser")

    if args.slack:
        webhook = args.slack_url or os.environ.get("SLACK_WEBHOOK_URL", "")
        if not webhook:
            print("  ✗ --slack requires SLACK_WEBHOOK_URL env var or --slack-url")
        else:
            print("\n  posting to Slack...")
            post_slack(scored, webhook, args.pages_url)

    print("\n── done ────────────────────────────────────")
    print(f"  {len(scored)} articles · open {args.output}")

if __name__ == "__main__":
    main()
