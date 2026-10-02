#!/usr/bin/env python3
"""funnel.py — batch resource triage: theme → content → consumption.

The command-line face of the wide funnel. Give it a corpus, get back
queue-ready rows plus a one-screen report:

  uv run python3 funnel.py --file ~/youtube_learning_timeline.md --limit 50
  cat urls.txt | uv run python3 funnel.py --stdin
  uv run python3 funnel.py --file signals.md --out queue.json --write-queue

Intake formats (auto-detected per line):
  - timeline  : - **[YYYY-MM-DD HH:MM]** [Title](url) — *channel*
  - tabbed    : Title<TAB>URL
  - plain URL : https://...

Output:
  - screen report: counts by theme / verdict / depth, plus a "needs human
    eyes" list (anything not routed 'act' — confidence between 0.60–0.85 or
    below).
  - --out PATH.json : queue-ready emit() rows (scrobbler queue schema shape).

Judgment honesty:
  - Default runs OFFLINE with a stand-in oracle. Verdicts are placeholder
    patterns, NOT real judgment — fine for plumbing, wrong for decisions.
  - --live requires typesafe_sdk + TYPESAFE_API_KEY; until then the report
    header says "STAND-IN" loudly.
  - --write-queue writes rows to the scrobbler queue table. It is opt-in on
    purpose: don't fill a real queue with stand-in verdicts.

Exit codes: 0 ok, 1 no resources found, 2 live mode requested but unavailable.
"""

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from resource_triage_adapter import (
    DEFAULT_THEMES,
    ScriptedClient,
    TriagePipeline,
)

TIMELINE_LINE = __import__("re").compile(
    r"\*\*\[([-\d]+ [\d:]+)\]\*\* \[([^\]]+)\]\((https?://[^)]+)\) — \*([^*]+)\*"
)

DEFAULT_SAMPLE = [
    "- **[2026-09-28 18:12]** [1970s planer mill restoration: full engine teardown](https://www.youtube.com/watch?v=REALID001) — *workshop-archive*",
    "- **[2026-09-26 09:40]** [How vLLM batches continuous batching under the hood](https://www.youtube.com/watch?v=REALID002) — *ml-systems*",
    "- **[2026-09-21 21:03]** [13 signs AI is eating the economy](https://www.youtube.com/watch?v=REALID003) — *opinion-rush*",
    "- **[2026-09-19 07:55]** [Halifax rental market: October numbers walkthrough](https://www.youtube.com/watch?v=REALID004) — *east-coast-property*",
    "- **[2026-09-15 14:27]** [Pallet flip walkthrough: 42 units, 71% sell-through](https://www.youtube.com/watch?v=REALID005) — *liquidation-ledger*",
]

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, content_type TEXT, url TEXT, author TEXT,
    priority INTEGER, tags TEXT, notes TEXT, status TEXT DEFAULT 'pending',
    source TEXT, created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
)
"""


# ── intake ─────────────────────────────────────────────────────────────

def _to_resource(kind, fields):
    if kind == "timeline":
        when, title, url, channel = fields
        return {"id": url.rsplit("=", 1)[-1][:12], "title": title, "url": url,
                "content_type": "youtube", "source": "timeline",
                "channel": channel, "captured_at": when}
    if kind == "tabbed":
        title, url = fields[0], fields[1]
        return {"id": url.rsplit("=", 1)[-1][:12], "title": title, "url": url,
                "content_type": "misc", "source": "list", "channel": ""}
    title, url = "untitled", fields[0]
    return {"id": url.rsplit("=", 1)[-1][:12], "title": title, "url": url,
            "content_type": "misc", "source": "url", "channel": ""}


def load_lines(lines, limit=0):
    """Auto-detect each line's shape; returns list of resource dicts."""
    out = []
    for raw in lines:
        raw = raw.rstrip("\n")
        if not raw.strip():
            continue
        m = TIMELINE_LINE.search(raw)
        if m:
            out.append(_to_resource("timeline", m.groups()))
        elif "\t" in raw:
            parts = raw.split("\t")
            out.append(_to_resource("tabbed", (parts[0].strip(), parts[1].strip())))
        elif raw.lstrip().startswith(("http://", "https://")):
            out.append(_to_resource("url", (raw.strip(),)))
        else:
            continue  # unknown shape — skip silently
        if limit and len(out) >= limit:
            break
    return out


# ── stand-in judgment ──────────────────────────────────────────────────

def canned_script(n):
    """One verdict pattern per resource — placeholder, not judgment."""
    themes = ["ai_agents", "making_cad", "markets_arbitrage",
              "real_estate", "industrial_ops", "logistics"]
    depths = ["deep", "deep", "solid", "solid", "fluff", "noise"]
    acts = ["consume_now", "queue", "queue", "queue", "skip", "skip"]
    prios = ["P1", "P2", "P3", "P4", "P5", "P5"]
    confs = [0.9, 0.85, 0.8, 0.75, 0.6, 0.5]

    def pack(choice, prob, conf, noul, score):
        return (choice, {choice: prob}, conf, noul, score)

    return {
        "theme": [pack(themes[i % 6], confs[i % 6], confs[i % 6], 0.8, None)
                  for i in range(n)],
        "content": [pack(depths[i % 6], confs[i % 6], confs[i % 6],
                         0.7 if depths[i % 6] != "fluff" else 0.3, "generic")
                    for i in range(n)],
        "consume": [pack(acts[i % 6], confs[i % 6], confs[i % 6],
                         0.75 if acts[i % 6] != "skip" else 0.3, prios[i % 6])
                    for i in range(n)],
    }


def real_client():
    """Live Jev client — only when the SDK and key are actually present."""
    try:
        from typesafe_sdk import Client  # type: ignore
    except ImportError:
        return None
    key = os.environ.get("TYPESAFE_API_KEY", "")
    if not key:
        return None
    return Client(api_key=key)


# ── queue persistence ──────────────────────────────────────────────────

def write_queue(rows, db_path, tag="funnel"):
    """Insert emit() rows into a queue table. Returns number written."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.executescript(QUEUE_SCHEMA)
    n = 0
    for row in rows:
        pri = row.get("priority")
        pri = int(pri.lstrip("P")) if isinstance(pri, str) and pri.startswith("P") else None
        conn.execute(
            "INSERT INTO queue (title, content_type, url, author, priority,"
            " tags, notes, status, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (row.get("title"), row.get("content_type"), row.get("url"),
             row.get("author"), pri, row.get("tags"), row.get("notes"),
             "pending", f"{tag}:{row.get('source', '')}"),
        )
        n += 1
    conn.commit()
    conn.close()
    return n


# ── CLI ────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Batch resource triage (theme → content → consumption)")
    ap.add_argument("--file", help="corpus file (timeline / tabbed / URL lines)")
    ap.add_argument("--stdin", action="store_true", help="read URL lines from stdin")
    ap.add_argument("--limit", type=int, default=0, help="max resources (0 = all)")
    ap.add_argument("--out", help="write queue-ready rows to PATH.json")
    ap.add_argument("--write-queue", metavar="DB", nargs="?", const="~/.hermes/personal_life.db",
                    help="insert rows into a queue table (opt-in)")
    ap.add_argument("--live", action="store_true", help="real Jev judgment (needs typesafe_sdk + TYPESAFE_API_KEY)")
    ap.add_argument("--db", default="triage_funnel.db", help="grid db path")
    args = ap.parse_args(argv)

    if args.file:
        if not os.path.exists(args.file):
            print(f"funnel: no such file: {args.file}", file=sys.stderr)
            return 1
        with open(args.file, errors="ignore") as fh:
            resources = load_lines(fh, args.limit)
    elif args.stdin:
        resources = load_lines(sys.stdin, args.limit)
    else:
        resources = load_lines(DEFAULT_SAMPLE, args.limit)

    if not resources:
        print("funnel: no resources found. Use --file, --stdin, or leave both off for the sample corpus.")
        return 1

    client = real_client() if args.live else None
    if args.live and client is None:
        print("funnel: --live requested but typesafe_sdk/TYPESAFE_API_KEY unavailable.", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as td:
        grid = os.path.join(td, "grid.db")
        pipe = TriagePipeline(db_path=grid if not args.live else args.db,
                              client=client or ScriptedClient(canned_script(len(resources))),
                              themes=DEFAULT_THEMES)
        results = [pipe.triage(r) for r in resources]

    rows = [t.emit() for t in results]
    mode = "LIVE" if args.live else "STAND-IN (placeholder verdicts — plumbing only)"

    themes = Counter(t.theme for t in results)
    verdicts = Counter(t.verdict for t in results)
    depths = Counter(t.depth for t in results)
    human = [t for t in results if t.route != "act"]

    print(f"funnel: {len(resources)} resources | judgment: {mode}")
    print(f"  themes    : " + "  ".join(f"{k}:{v}" for k, v in themes.most_common()))
    print(f"  verdicts  : " + "  ".join(f"{k}:{v}" for k, v in verdicts.most_common()))
    print(f"  depth     : " + "  ".join(f"{k}:{v}" for k, v in depths.most_common()))
    print(f"  needs human eyes: {len(human)}/{len(results)}")
    for t in human[:10]:
        print(f"    - {t.resource.get('title', '')[:56]:<56} route={t.route}")

    if args.out:
        with open(args.out, "w") as fh:
            json.dump(rows, fh, indent=2)
        print(f"  wrote {len(rows)} queue-ready rows -> {args.out}")

    if args.write_queue:
        path = os.path.expanduser(args.write_queue)
        n = write_queue(rows, path, tag="funnel")
        print(f"  wrote {n} rows into queue table @ {path}  (opt-in)")

    return 0


if __name__ == "__main__":
    sys.exit(main())