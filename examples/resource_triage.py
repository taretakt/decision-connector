#!/usr/bin/env python3
"""Real-data example — run the resource funnel against a timeline.

Two modes:

  uv run python3 examples/resource_triage.py               # 5 sample entries
  YT_TIMELINE=~/youtube_learning_timeline.md uv run ...    # your real corpus

The timeline format is the one this example knows how to read:

  - **[YYYY-MM-DD HH:MM]** [Title](url) — *channel* (`Acc N`)

Judgment runs OFFLINE with a stand-in oracle so anyone can run this without
keys. To go live, replace the ScriptedClient with a real Jev client — the
pipeline code below does not change.

The queue row emitted per resource is shaped for a queue table
(title, url, content_type, source, tags, priority, verdict).
"""

import os
import re
import sys
import tempfile
from pathlib import Path

# Examples live one level down; make the repo root importable regardless of
# how the script is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resource_triage_adapter import DEFAULT_THEMES, TriagePipeline, ScriptedClient

TIMELINE_LINE = re.compile(
    r"\*\*\[([-\d]+ [\d:]+)\]\*\* \[([^\]]+)\]\((https?://[^)]+)\) — \*([^*]+)\*"
)

# Inline corpus so the example runs anywhere, with zero external files.
SAMPLE = [
    "- **[2026-09-28 18:12]** [1970s planer mill restoration: full engine teardown](https://www.youtube.com/watch?v=REALID001) — *workshop-archive* (`Acc 1`)",
    "- **[2026-09-26 09:40]** [How vLLM batches continuous batching under the hood](https://www.youtube.com/watch?v=REALID002) — *ml-systems* (`Acc 1`)",
    "- **[2026-09-21 21:03]** [13 signs AI is eating the economy](https://www.youtube.com/watch?v=REALID003) — *opinion-rush* (`Acc 2`)",
    "- **[2026-09-19 07:55]** [Halifax rental market: October numbers walkthrough](https://www.youtube.com/watch?v=REALID004) — *east-coast-property* (`Acc 1`)",
    "- **[2026-09-15 14:27]** [Pallet flip walkthrough: 42 units, 71% sell-through](https://www.youtube.com/watch?v=REALID005) — *liquidation-ledger* (`Acc 2`)",
]


def load_timeline(path: Path, limit: int) -> list[dict]:
    entries = []
    for raw in path.read_text(errors="ignore").splitlines():
        m = TIMELINE_LINE.search(raw)
        if not m:
            continue
        when, title, url, channel = m.groups()
        entries.append({
            "id": url.rsplit("=", 1)[-1][:12],
            "title": title,
            "url": url,
            "content_type": "youtube",
            "source": "youtube_timeline",
            "channel": channel,
            "captured_at": when,
        })
        if len(entries) >= limit:
            break
    return entries


def canned_script(n: int) -> dict:
    """One verdict pattern per resource — offline stand-in judgment."""
    themes = ["ai_agents", "making_cad", "markets_arbitrage",
              "real_estate", "industrial_ops", "logistics"]
    depths = ["deep", "deep", "solid", "solid", "fluff", "noise"]
    acts = ["consume_now", "queue", "queue", "queue", "skip", "skip"]
    prios = ["P1", "P2", "P3", "P4", "P5", "P5"]
    confs = [0.9, 0.85, 0.8, 0.75, 0.6, 0.5]

    def pack(choice, prob, conf, noul, score):
        return (choice, {choice: prob}, conf, noul, score)

    return {
        "theme": [pack(themes[i % len(themes)], confs[i % len(confs)],
                       confs[i % len(confs)], 0.8, None) for i in range(n)],
        "content": [pack(depths[i % len(depths)], confs[i % len(confs)],
                         confs[i % len(confs)], 0.7 if depths[i % 6] != "fluff" else 0.3,
                         "generic") for i in range(n)],
        "consume": [pack(acts[i % len(acts)], confs[i % len(confs)],
                         confs[i % len(confs)], 0.75 if acts[i % 6] != "skip" else 0.3,
                         prios[i % len(prios)]) for i in range(n)],
    }


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(SAMPLE)
    corpus = os.environ.get("YT_TIMELINE")
    if corpus:
        entries = load_timeline(Path(corpus), limit)
        print(f"corpus: {corpus} ({len(entries)} entries)")
    else:
        entries = []
        for i, line in enumerate(SAMPLE[:limit]):
            m = TIMELINE_LINE.search(line)
            entries.append({
                "id": f"sample{i:02d}", "title": m.group(2), "url": m.group(3),
                "content_type": "youtube", "source": "sample", "channel": m.group(4),
                "captured_at": m.group(1),
            })
        print(f"corpus: inline sample ({len(entries)} entries) — set YT_TIMELINE for real data")

    with tempfile.TemporaryDirectory() as td:
        pipe = TriagePipeline(
            db_path=os.path.join(td, "triage.db"),
            client=ScriptedClient(canned_script(len(entries))),
            themes=DEFAULT_THEMES,
        )
        for res in entries:
            t = pipe.triage(res)
            row = t.emit()
            print(f"  {row['title'][:52]:<52} {t.verdict:<11} {row['priority']:<3} "
                  f"{t.theme:<18} {t.depth}")

        print(f"\nJEV CALLS: {3 * len(entries)} (3 per resource, offline stand-in)")
        print("Queue rows above are emit()-ready for a queue table.")


if __name__ == "__main__":
    main()