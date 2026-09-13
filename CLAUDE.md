# Project context for Claude Code

A pair of personal scripts for analysing Cambridge CS Tripos past-paper
coverage of a given topic. Used by me for exam prep — keep changes
proportionate; this is not production software.

## Files

- `tripos_topic.py` — scrape a tripos course page, upload PDFs via the
  Anthropic Files API, score each question 0-3 against a topic with Claude.
- `tripos_browse.py` — Textual TUI to browse the resulting JSON files.
- `requirements.txt` — `anthropic`, `beautifulsoup4`, `requests`,
  `python-dotenv`, `textual`.
- `.env.example` — template for the API key (loaded via `python-dotenv`).
- `README.md` — user-facing docs.

## Setup

API key in `.env` at the project root. `python tripos_topic.py --list-courses`
prints the hardcoded course list; first positional arg accepts course names,
unambiguous substrings, or full URLs. `resolve_course` also fuzzy-matches:
after substring it tries subsequence (abbreviations like `concdist`) then a
`difflib` similarity pass (typos), but only commits when there's a clear
winner — ambiguous input raises with a "did you mean…" hint rather than
guessing (a wrong course = real API spend).

## Key design decisions (do not re-litigate)

- **Files API not base64.** I may re-ask different topics against the same
  corpus, so PDFs are uploaded once and referenced by `file_id`. The
  `files_manifest.json` maps `paper_id → file_id`. Note: this saves **upload
  bandwidth**, not tokens — referencing a `file_id` still bills the PDF as
  input tokens on every call. Do not claim otherwise.
- **Rubric in `SYSTEM_PROMPT`** at the top of `tripos_topic.py` is the
  load-bearing piece. Score is 0-3, returned via forced `tool_use` against the
  `report_relevance` schema. Tweaking the rubric wording is fine; changing the
  schema shape needs care because the TUI parses these fields.
- **Output naming is automatic** from the course URL + topic slug:
  `results/<CourseName>_<topic-slug>.json` (the `results/` subfolder, constant
  `RESULTS_DIR`, is created on write). Override the full path with `--out`.
  Slugifier strips non-ASCII (e.g. λ-calculus → calculus); users override with
  `--out` when this bites. The TUI reads from `results/`, falling back to `.`
  if that folder is absent (`results_dir()`).
- **Resume semantics.** Same `--out` + same topic = skip already-analysed IDs
  and append. Same `--out` + different topic = overwrite (with a warning).
  Re-running with the same `--manifest` across different topics reuses uploads.
- **Cumulative cost log** at `~/.tripos_topic_usage.json`. Per-run entries
  store `cost_usd` as computed at that time, so updating pricing constants
  later does not retroactively rewrite history.
- **Pricing constants are verified.** Sonnet 4.6 is $3/Mtok in, $15/Mtok out.
  Do not re-add "verify pricing" warnings to printouts or comments.
- **Cost confirmation is a loop, not y/N.** At the estimate prompt the user
  can type `y` (run API analysis), a positive integer to recap to that many
  most-recent *years* and re-prompt, or `q` to abort. `n`/empty (the default)
  runs the **local fuzzy search** instead of aborting — by design. The cap
  applies to the full scrape, not the previously-capped subset.
- **`--latest N` / prompt cap is in years, not questions.** Windowing keeps
  questions from the N most-recent distinct exam years (`window_by_years`), so
  it pulls whole years rather than a fixed question count. The scrape printout
  and the confirm prompt list the distinct years present.
- **Local fuzzy search (`--local` / prompt default).** Scores questions by
  matching the topic against PDF text (`pypdf`) instead of the API — free,
  offline, no key. `" or "` splits the topic into alternatives; score is the
  match count capped at 3 (proportionate, keeps the 0–3 schema the TUI parses);
  reasoning is the count + a context snippet. Output carries a `.local` marker
  in the filename and records `model: local-fuzzy-search`. `--local` skips the
  cost prompt; the interactive default also routes here. `pypdf` is imported
  lazily so the API path doesn't require it.
- **Concurrency is `asyncio.Semaphore`-bounded.** Default 5. Per-call usage
  tokens are accumulated in a `RunUsage` dataclass.
- **PDF cache** at `./pdf_cache/` is the local copy. We also upload to Files
  API. Both layers exist so re-uploads after an Anthropic-side file deletion
  don't require re-downloading from cl.cam.ac.uk.

## Things deliberately not done

- **No programmatic pricing fetch.** Anthropic doesn't expose model pricing
  via API. Community sources (LiteLLM JSON) exist but add a brittle dependency
  for a value that changes ~once a year. Two-line constants win.
- **No "credit balance on startup" in TUI.** No Anthropic endpoint returns
  current credit. The Admin API returns historical *spend*, not *balance*, and
  needs an admin key. Not worth it.
- **No prompt caching.** Cache TTL (5 min default, 1 hour max) doesn't match
  the "re-ask next week" usage pattern. Reconsider if doing many topics
  back-to-back in one session.
- **No per-topic scoring for compound queries.** `"x or y"` works fine as a
  topic string — Claude handles natural-language disjunction — but the score
  is a max across both, not a per-topic breakdown. Adding per-topic scores
  would mean a richer tool schema (`list[{topic, score}]`); easy to do if
  asked, but the current design is intentional.

## Possible extensions (not blocked, just unbuilt)

- Fish shell completions for course names.
- Search across multiple result files in the TUI (currently search filters
  only the current file's table).
- Multi-topic comparison output (would require the tool-schema change above).
- Batch API support — 50% cheaper, but trades latency for cost; would change
  the resume/incremental-write story.

## Conventions

- Python 3.10+ features fine (`from __future__ import annotations` already in
  use; `dict[str, str]` style hints throughout).
- Sync for I/O setup phases (scrape, download, upload). Async only for the
  analysis fan-out.
- Incremental writes: result JSON is rewritten after every successful
  analysis so a crash mid-run loses nothing.
- Default model: `claude-sonnet-4-6`. Opus is overkill for this rubric task.
- No tests yet. If adding them, mock the Anthropic client at the
  `messages.create` / `beta.files.upload` boundary.
