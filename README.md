# tripos-topic

Score how often a topic comes up in Cambridge CS Tripos past papers, using the
Claude API. Two scripts: one to scrape + analyse, one to browse the results.

## Setup

```sh
pip install -r requirements.txt
cp .env.example .env       # then add your real ANTHROPIC_API_KEY
chmod 600 .env
```

The key lives in `.env`; `python-dotenv` loads it on startup. A shell-exported
`ANTHROPIC_API_KEY` takes precedence if set.

## Analyse a course page

```sh
python tripos_topic.py "<course>" "<topic>"
```

The first arg accepts any of:

- A full course name: `"Logic and Proof"`
- Any unambiguous substring: `"hoare"`, `"cyber"`, `"denotational"`
- A fuzzy match: abbreviations like `"concdist"` or typos like
  `"logick and prof"` resolve when there's a clear winner; otherwise you get a
  "did you mean…" hint
- A full URL (escape hatch for pages outside the hardcoded list)

Run `python tripos_topic.py --list-courses` to see the available names.

Example:

```sh
python tripos_topic.py "Logic and Proof" "natural deduction"
```

Each tripos question is scored 0-3 against the topic:

- **0** absent
- **1** incidental mention or one minor sub-part
- **2** significant component, multiple parts depend on it
- **3** the question is primarily about the topic

The rubric lives in `SYSTEM_PROMPT` near the top of `tripos_topic.py` — tweak
it and re-run if scores feel off-calibration.

### Local fuzzy search (no API)

To skip the Claude API entirely, score questions by a local fuzzy text search
of the PDFs instead — free, offline, no API key needed:

```sh
python tripos_topic.py "Logic and Proof" "herbrand" --local
```

You also get this without the flag: at the cost prompt, **`n` (the default)
runs the local search** instead of doing nothing (`y` = API, a number = cap,
`q` = abort).

Local scoring counts case/punctuation-insensitive matches of the topic
(`" or "` splits into alternatives, so `"logic T or logic S4"` matches either);
the score is the match count capped at 3, and the reasoning is the count plus a
snippet of context around the first match. Output is written to
`results/<CourseName>_<topic-slug>.local.json` — the `.local` marker keeps it
separate from API results, and the TUI shows `local-fuzzy-search` as the model.
Needs `pypdf` (in `requirements.txt`).

### Output

- `results/<CourseName>_<topic-slug>.json` — scores and reasoning per question
  (the `results/` subfolder is created automatically; override the whole path
  with `--out`). The browse tool reads from here.
- `pdf_cache/y####p#q#.pdf` — downloaded PDFs (reused across runs)
- `files_manifest.json` — Anthropic Files API IDs, reused across topics
- `~/.tripos_topic_usage.json` — cumulative log: every run with its tokens
  and computed cost, plus running totals. Printed after each run.

Re-running with the **same topic** resumes (skips analysed questions). Re-running
with a **different topic** on the same corpus reuses uploaded files so you skip
the re-upload step — but note this saves bandwidth, not tokens. Referencing a
`file_id` still bills the PDF as input tokens on every analysis call.

### Options

| flag | default | purpose |
|---|---|---|
| `--out PATH` | auto from URL + topic | results JSON path |
| `--cache DIR` | `./pdf_cache` | local PDF cache |
| `--manifest PATH` | `./files_manifest.json` | Files API ID map |
| `--model NAME` | `claude-sonnet-4-6` | Claude model |
| `--concurrency N` | `5` | concurrent API calls |
| `--latest N` | all | only analyse questions from the N most recent years |
| `--usage-log PATH` | `~/.tripos_topic_usage.json` | cumulative cost log |
| `-y` | off | skip cost-estimate prompt |
| `--local` | off | score via local fuzzy PDF search, no API (see above) |

## Browse results

```sh
python tripos_browse.py
```

Auto-discovers tripos-format JSON in `results/` (falling back to the current
directory if there's no `results/` folder). The header shows cumulative API
spend read from `~/.tripos_topic_usage.json` (refreshed on `r`).

| key | action |
|---|---|
| ↑ ↓ / click | navigate |
| `f` | focus file search — filter the file list by name |
| `/` | focus question search — filter the current file's questions by paper ID + reasoning |
| `Esc` | clear the focused search box |
| 0 / 1 / 2 / 3 | filter table to score ≥ N |
| o | open highlighted question's PDF in browser |
| `Shift+O` | open every PDF currently shown in the table (respects the score + search filters) |
| r | refresh file list |
| q | quit |

The two searches are independent: `f` narrows which files show in the sidebar,
`/` narrows which questions show in the table for the selected file. File
search is fuzzy — it tries substring first, then falls back to abbreviations
(`logherb` → `LogicandProof_herbrand`) and typos, ranked by closeness.

Questions from the open-book exam years (2020–2022) are flagged with a 📖
marker in the table, a count in the header, and a badge in the reasoning pane.
`tripos_topic.py` likewise tags those papers and prints a reminder in its
summary.

## Notes

- **Pricing constants** at the top of `tripos_topic.py` are placeholders —
  verify against current rates at <https://docs.claude.com> before trusting the
  cost estimate.
- **Files API persistence**: uploaded PDFs sit in your Anthropic org storage
  until you `DELETE /v1/files/{id}` them. `files_manifest.json` has every ID
  ready for a cleanup sweep.
- **Server-side logs**: Anthropic retains API inputs/outputs for 7 days by
  default.
- Gitignore `.env`, `pdf_cache/`, and `files_manifest.json` if this lives near
  a repo.
