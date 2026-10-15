---
name: yc-office-hours
description: >-
  Searches the YC Startup Library and Paul Graham essays, including Startup
  School, Office Hours, Lightcone, and Garry Tan. Use when the user asks what
  YC or Paul Graham says about startups, founders, hiring, growth, or product.
  Do not use for the YC company directory or for ordinary coding tasks.
---

# YC Office Hours

Run the commands from the directory that contains this file. After a global install, that directory is `~/.agents/skills/yc-office-hours`.

## Steps

1. Refresh the local cache. The command writes to `~/.cache/yc-office-hours` and downloads from ycombinator.com and paulgraham.com, so it needs permission to write outside the project and to use the network. If it fails because it cannot write there, run it again outside the sandbox.

```bash
python3 scripts/sync.py
```

2. If the output says `BUILDING`, tell the user the first download takes a few minutes, then wait for it. `FRESH` means the cache was already current. If sync is still running and did not print `BUILDING`, it is downloading new talks, or the monthly essay refresh. Say so if it takes more than a moment. If it says `BACKLOG`, run sync once more, then search with what is already saved.

3. If sync exits 2 and `~/.cache/yc-office-hours` has files, say the scrape broke, include `carousel_fetched_at` from `~/.cache/yc-office-hours/state.json`, and continue. If that directory is missing or empty, stop.

4. Search. This also writes the index into `~/.cache/yc-office-hours`, so it needs the same permission to write outside the project.

```bash
python3 scripts/search.py "the question"
```

5. Answer from the snippets. Open a full file only when the snippet is not enough, and open at most two. Cite each sourced claim with its `source_url`. Use `youtube_url` when it is present. Do not invent a timestamp the result does not include.

6. If the search returns no hits, say the cache did not contain an answer.
