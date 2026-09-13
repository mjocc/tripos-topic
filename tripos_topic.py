#!/usr/bin/env python3
"""tripos_topic.py — scrape a Cambridge tripos past-papers index page, upload
the question PDFs once via the Anthropic Files API, and score each question's
relevance (0-3) to a given topic with Claude.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python tripos_topic.py <URL> "<topic>" [options]

Example:
    python tripos_topic.py \
        https://www.cl.cam.ac.uk/teaching/exams/pastpapers/t-LogicandProof.html \
        "natural deduction"

Re-running with the same --out and same topic resumes (skips analysed IDs).
Re-running with a different topic but the same --manifest reuses uploaded
files, so you only pay for the analysis tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

try:
    from pypdf import PdfReader
except ImportError:  # only needed for --local; analysis path doesn't touch it
    PdfReader = None
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv


# ---- Configuration -----------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-6"
FILES_BETA = "files-api-2025-04-14"


def link(text: str, url: str) -> str:
    """Wrap text in an OSC 8 terminal hyperlink so it's clickable in the
    terminal. Falls back to plain text when there's no URL or stdout isn't a
    tty (e.g. piped or redirected), so logs stay clean."""
    if not url or not sys.stdout.isatty():
        return text
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\"
PDF_HREF_RE = re.compile(r"y(\d{4})p(\d+)q(\d+)\.pdf", re.IGNORECASE)
USER_AGENT = "tripos_topic.py/1.0 (personal research script)"

TRIPOS_BASE_URL = "https://www.cl.cam.ac.uk/teaching/exams/pastpapers/"

# Hardcoded list of Computer Science Tripos courses (display name -> page suffix).
COURSES: dict[str, str] = {
    "Advanced Computer Architecture": "t-AdvancedComputerArchitecture.html",
    "Algorithms 1": "t-Algorithms1.html",
    "Algorithms 2": "t-Algorithms2.html",
    "Artificial Intelligence": "t-ArtificialIntelligence.html",
    "Bioinformatics": "t-Bioinformatics.html",
    "Business Studies": "t-BusinessStudies.html",
    "Compiler Construction": "t-CompilerConstruction.html",
    "Complexity Theory": "t-ComplexityTheory.html",
    "Computation Theory": "t-ComputationTheory.html",
    "Computer Networking": "t-ComputerNetworking.html",
    "Concepts in Programming Languages": "t-ConceptsinProgrammingLanguages.html",
    "Concurrent and Distributed Systems": "t-ConcurrentandDistributedSystems.html",
    "Cryptography": "t-Cryptography.html",
    "Cybersecurity": "t-Cybersecurity.html",
    "Data Science": "t-DataScience.html",
    "Databases": "t-Databases.html",
    "Denotational Semantics": "t-DenotationalSemantics.html",
    "Digital Electronics": "t-DigitalElectronics.html",
    "Discrete Mathematics": "t-DiscreteMathematics.html",
    "E-Commerce": "t-E-Commerce.html",
    "Economics, Law and Ethics": "t-EconomicsLawandEthics.html",
    "Formal Models of Language": "t-FormalModelsofLanguage.html",
    "Foundations of Computer Science": "t-FoundationsofComputerScience.html",
    "Further Graphics": "t-FurtherGraphics.html",
    "Further Human-Computer Interaction": "t-FurtherHuman-ComputerInteraction.html",
    "Hoare Logic and Model Checking": "t-HoareLogicandModelChecking.html",
    "Information Theory": "t-InformationTheory.html",
    "Interaction Design": "t-InteractionDesign.html",
    "Introduction to Computer Architecture": "t-IntroductiontoComputerArchitecture.html",
    "Introduction to Graphics": "t-IntroductiontoGraphics.html",
    "Introduction to Probability": "t-IntroductiontoProbability.html",
    "Logic and Proof": "t-LogicandProof.html",
    "Machine Learning and Bayesian Inference": "t-MachineLearningandBayesianInference.html",
    "Machine Learning and Real-world Data": "t-MachineLearningandReal-worldData.html",
    "Object-Oriented Programming": "t-Object-OrientedProgramming.html",
    "Operating Systems": "t-OperatingSystems.html",
    "Optimising Compilers": "t-OptimisingCompilers.html",
    "Principles of Communications": "t-PrinciplesofCommunications.html",
    "Programming in C and C++": "t-ProgramminginCandC++.html",
    "Prolog": "t-Prolog.html",
    "Quantum Computing": "t-QuantumComputing.html",
    "Randomised Algorithms": "t-RandomisedAlgorithms.html",
    "Semantics of Programming Languages": "t-SemanticsofProgrammingLanguages.html",
    "Software and Security Engineering": "t-SoftwareandSecurityEngineering.html",
    "Types": "t-Types.html",
}


def _normalise(s: str) -> str:
    """Lowercase and strip everything that isn't alphanumeric."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _is_subsequence(needle: str, haystack: str) -> bool:
    """True if every char of needle appears in haystack in order (gaps allowed)."""
    it = iter(haystack)
    return all(c in it for c in needle)


def resolve_course(query: str) -> str:
    """Turn a URL, course name, or fuzzy course name into a full URL.

    Resolution order (first decisive step wins):
      1. Already a URL (contains '://') -> return as-is.
      2. Bare HTML filename -> prepend the tripos base URL.
      3. Exact case/punctuation-insensitive name match.
      4. Unique case-insensitive substring match against display names.
      5. Unique subsequence match (abbreviations: 'concdist', 'logicproof').
      6. Best difflib similarity match if it clears a threshold and is a
         clear winner (typos: 'logick and prof').
      7. Otherwise raise, suggesting the closest names.
    """
    if "://" in query:
        return query
    if query.endswith(".html"):
        return TRIPOS_BASE_URL + query

    norm_query = _normalise(query)
    norm = {name: _normalise(name) for name in COURSES}
    by_norm = {n: name for name, n in norm.items()}
    if norm_query in by_norm:
        return TRIPOS_BASE_URL + COURSES[by_norm[norm_query]]

    q_lower = query.lower()
    matches = [name for name in COURSES if q_lower in name.lower()]
    if len(matches) == 1:
        return TRIPOS_BASE_URL + COURSES[matches[0]]
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous course '{query}' matches {len(matches)}: "
            + ", ".join(matches)
        )

    # 5. Subsequence match — cheap win for abbreviations.
    subseq = [name for name in COURSES if _is_subsequence(norm_query, norm[name])]
    if len(subseq) == 1:
        return TRIPOS_BASE_URL + COURSES[subseq[0]]

    # 6. Fuzzy similarity. Restrict to subsequence hits if any, else the full
    #    list, then require a decent score and a clear gap to the runner-up.
    pool = subseq or list(COURSES)
    ranked = sorted(
        pool,
        key=lambda name: difflib.SequenceMatcher(None, norm_query, norm[name]).ratio(),
        reverse=True,
    )
    top_r = difflib.SequenceMatcher(None, norm_query, norm[ranked[0]]).ratio()
    second_r = (
        difflib.SequenceMatcher(None, norm_query, norm[ranked[1]]).ratio()
        if len(ranked) > 1 else 0.0
    )
    if top_r >= 0.6 and top_r - second_r >= 0.1:
        return TRIPOS_BASE_URL + COURSES[ranked[0]]

    suggestions = [name for name in ranked[:3] if
                   difflib.SequenceMatcher(None, norm_query, norm[name]).ratio() >= 0.4]
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ValueError(
        f"No course matches '{query}'.{hint} Use --list-courses to see options."
    )


def list_courses() -> None:
    print(f"{len(COURSES)} tripos courses:")
    for name in COURSES:
        print(f"  {name}")


# Years the Tripos exams were sat open-book (COVID-era remote exams).
OPEN_BOOK_YEARS = {2020, 2021, 2022}


# Pricing constants for the default model (Sonnet 4.6): $3/Mtok in, $15/Mtok out.
PRICE_IN_PER_MTOK = 3.0     # $ per million input tokens
PRICE_OUT_PER_MTOK = 15.0   # $ per million output tokens
EST_INPUT_TOKENS_PER_PDF = 2000
EST_OUTPUT_TOKENS_PER_CALL = 200


SYSTEM_PROMPT = """\
You are scoring Cambridge Computer Science Tripos exam questions for their \
relevance to a given topic. The score answers one practical question: if I \
study this topic, will that help me answer this exam question? Use the \
report_relevance tool with this rubric:

  0 = Studying the topic would not help with this question. It may even be in \
the same broad subject area, but answering it draws on different knowledge.
  1 = Studying the topic would genuinely help with part of this question, even \
though the question isn't mainly about the topic and may never name it. \
Knowing the topic gives you a real handle on at least one sub-part.
  2 = Significant component — multiple sub-parts depend on the topic; studying \
it clearly pays off here.
  3 = The question is primarily about the topic; studying it is essential.

The 1 tier is about usefulness, not mere subject-area overlap. Ask concretely: \
"would knowing this topic let me answer something here I otherwise couldn't?" \
If yes, score 1. If the question is merely nearby in the syllabus but its \
sub-parts rest on other material, score 0 — do not flag a question just \
because it shares a lecture course or a few keywords with the topic.

For example, for the topic "CUDA", a question on why GPUs need high-bandwidth \
memory with sub-parts about warps scores 1: you genuinely need GPU-execution \
knowledge to answer it, despite no mention of CUDA. By contrast, a question \
that only brushes past the topic's area in passing — where actually studying \
the topic wouldn't help you answer any sub-part — scores 0.

Keep the top of the scale strict: only score 2 or 3 when understanding the \
topic itself is genuinely needed (2 for a significant part, 3 when the \
question is mostly about it). A passing mention or a mere example involving \
the topic is a 1, not a 2."""


RELEVANCE_TOOL = {
    "name": "report_relevance",
    "description": "Report how relevant this exam question is to the given topic.",
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "enum": [0, 1, 2, 3],
                "description": "Relevance score per the rubric in the system prompt.",
            },
            "reasoning": {
                "type": "string",
                "description": "1-3 sentences citing specific sub-parts or content that justify the score.",
            },
        },
        "required": ["score", "reasoning"],
    },
}


# ---- Data types --------------------------------------------------------------

@dataclass(frozen=True)
class Paper:
    paper_id: str  # e.g. "y2023p6q3"
    year: int
    paper: int
    question: int
    pdf_url: str


@dataclass
class Result:
    paper_id: str
    year: int
    paper: int
    question: int
    pdf_url: str
    file_id: str
    score: int
    reasoning: str


# ---- Scraping ----------------------------------------------------------------

def scrape_links(page_url: str) -> list[Paper]:
    resp = requests.get(page_url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    found: dict[str, Paper] = {}
    for a in soup.find_all("a", href=True):
        m = PDF_HREF_RE.search(a["href"])
        if not m:
            continue
        year, paper, q = (int(x) for x in m.groups())
        paper_id = f"y{year}p{paper}q{q}"
        if paper_id in found:
            continue
        found[paper_id] = Paper(
            paper_id=paper_id,
            year=year,
            paper=paper,
            question=q,
            pdf_url=urljoin(page_url, a["href"]),
        )

    return sorted(found.values(), key=lambda p: (p.year, p.paper, p.question))


def unique_years(papers: list[Paper]) -> list[int]:
    """Distinct exam years present, ascending."""
    return sorted({p.year for p in papers})


def fmt_years(years: list[int]) -> str:
    return ", ".join(str(y) for y in years)


def window_by_years(papers: list[Paper], n_years: int) -> list[Paper]:
    """Keep only questions from the n_years most recent years. n_years <= 0
    (or larger than the number of years available) keeps everything."""
    if n_years <= 0:
        return papers
    keep = set(unique_years(papers)[-n_years:])
    return [p for p in papers if p.year in keep]


# ---- Local PDF cache ---------------------------------------------------------

def ensure_pdf_cached(paper: Paper, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{paper.paper_id}.pdf"
    if path.exists() and path.stat().st_size > 0:
        return path
    resp = requests.get(paper.pdf_url, timeout=60, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    path.write_bytes(resp.content)
    time.sleep(0.3)  # be courteous to cl.cam.ac.uk
    return path


# ---- Files API manifest ------------------------------------------------------

def load_manifest(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_manifest(path: Path, manifest: dict[str, str]) -> None:
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True))


def ensure_uploaded(
    client: anthropic.Anthropic,
    paper: Paper,
    pdf_path: Path,
    manifest: dict[str, str],
    manifest_path: Path,
) -> str:
    if paper.paper_id in manifest:
        return manifest[paper.paper_id]
    with pdf_path.open("rb") as f:
        uploaded = client.beta.files.upload(
            file=(pdf_path.name, f, "application/pdf"),
        )
    manifest[paper.paper_id] = uploaded.id
    save_manifest(manifest_path, manifest)  # persist after every upload
    return uploaded.id


# ---- Analysis ----------------------------------------------------------------

@dataclass
class RunUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, msg) -> None:
        u = getattr(msg, "usage", None)
        if u is None:
            return
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.calls += 1

    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * PRICE_IN_PER_MTOK
            + self.output_tokens / 1_000_000 * PRICE_OUT_PER_MTOK
        )


async def analyse(
    client: anthropic.AsyncAnthropic,
    paper: Paper,
    file_id: str,
    topic: str,
    model: str,
    semaphore: asyncio.Semaphore,
    usage: RunUsage,
) -> Optional[Result]:
    async with semaphore:
        try:
            msg = await client.messages.create(
                model=model,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                tools=[RELEVANCE_TOOL],
                tool_choice={"type": "tool", "name": "report_relevance"},
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {"type": "file", "file_id": file_id},
                        },
                        {
                            "type": "text",
                            "text": f"Topic: {topic}\n\nCall report_relevance for the attached question.",
                        },
                    ],
                }],
                extra_headers={"anthropic-beta": FILES_BETA},
            )
        except Exception as e:
            print(f"  ✗ {paper.paper_id}: {e}", file=sys.stderr)
            return None

    usage.add(msg)
    tool_block = next((b for b in msg.content if b.type == "tool_use"), None)
    if tool_block is None:
        print(f"  ✗ {paper.paper_id}: no tool_use in response", file=sys.stderr)
        return None

    score = int(tool_block.input["score"])
    reasoning = str(tool_block.input["reasoning"])
    ob = " [open-book]" if paper.year in OPEN_BOOK_YEARS else ""
    pid = link(paper.paper_id, paper.pdf_url)
    print(f"  {score} {pid}{ob} — {reasoning[:80]}{'…' if len(reasoning) > 80 else ''}")
    return Result(
        paper_id=paper.paper_id,
        year=paper.year,
        paper=paper.paper,
        question=paper.question,
        pdf_url=paper.pdf_url,
        file_id=file_id,
        score=score,
        reasoning=reasoning,
    )


# ---- Local fuzzy search (no API) ---------------------------------------------

# Sentinel model name recorded in local-search result files. The TUI shows this
# in its header, and the output filename carries a ".local" marker too.
LOCAL_MODEL = "local-fuzzy-search"


def extract_pdf_text(path: Path) -> str:
    """Whitespace-normalised text of a PDF. Empty string if nothing extractable."""
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed (pip install pypdf)")
    reader = PdfReader(str(path))
    raw = "\n".join(page.extract_text() or "" for page in reader.pages)
    return re.sub(r"\s+", " ", raw).strip()


def _topic_patterns(topic: str) -> list[re.Pattern]:
    """Compile one regex per ' or '-separated alternative in the topic.

    Each alternative's words are matched in order, tolerant of the punctuation
    and whitespace PDFs scatter between them (so 'fourier motzkin' matches
    'Fourier–Motzkin'). Case-insensitive.
    """
    pats: list[re.Pattern] = []
    for alt in re.split(r"\s+or\s+", topic.strip(), flags=re.IGNORECASE):
        tokens = re.findall(r"[a-zA-Z0-9]+", alt)
        if tokens:
            body = r"[\W_]+".join(re.escape(t) for t in tokens)
            pats.append(re.compile(rf"\b{body}\b", re.IGNORECASE))
    return pats


def local_score(text: str, topic: str) -> tuple[int, str]:
    """Score a PDF's text against a topic by counting fuzzy matches.

    Score is the match count capped at 3 (0 = absent, 3 = three or more
    mentions), keeping the same 0–3 scale the rubric and TUI expect. The
    reasoning is the count plus a snippet of context around the first match.
    """
    if not text:
        return 0, "No extractable text in PDF (likely scanned/image-only)."

    spans = sorted(
        (m.span() for pat in _topic_patterns(topic) for m in pat.finditer(text))
    )
    count = len(spans)
    if count == 0:
        return 0, f"No local match for '{topic}'."

    start, end = spans[0]
    lo, hi = max(0, start - 70), min(len(text), end + 70)
    snippet = text[lo:hi]
    if lo > 0:
        snippet = "…" + snippet
    if hi < len(text):
        snippet = snippet + "…"
    plural = "match" if count == 1 else "matches"
    return min(count, 3), f"{count} {plural} for '{topic}'. Context: {snippet}"


# ---- Output ------------------------------------------------------------------

def load_existing(path: Path) -> tuple[Optional[str], dict[str, Result]]:
    """Returns (topic, results_by_paper_id) or (None, {}) if absent."""
    if not path.exists():
        return None, {}
    data = json.loads(path.read_text())
    results = {r["paper_id"]: Result(**r) for r in data.get("results", [])}
    return data.get("topic"), results


def write_results(
    path: Path,
    topic: str,
    page_url: str,
    model: str,
    results: dict[str, Result],
) -> None:
    payload = {
        "topic": topic,
        "page_url": page_url,
        "model": model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": [
            asdict(r)
            for r in sorted(results.values(), key=lambda r: (r.year, r.paper, r.question))
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def print_summary(results: dict[str, Result]) -> None:
    counts = {0: 0, 1: 0, 2: 0, 3: 0}
    for r in results.values():
        counts[r.score] += 1
    total = sum(counts.values())
    print()
    print(f"Total analysed: {total}")
    for s in (3, 2, 1, 0):
        bar = "█" * counts[s]
        print(f"  score {s}: {counts[s]:>3}  {bar}")
    hits = sorted(
        (r for r in results.values() if r.score >= 2),
        key=lambda r: (-r.score, r.year, r.paper, r.question),
    )
    if hits:
        print(f"\nQuestions with score ≥ 2 ({len(hits)}):")
        for r in hits:
            ob = " [open-book]" if r.year in OPEN_BOOK_YEARS else ""
            print(f"  [{r.score}] {link(r.paper_id, r.pdf_url)}{ob}: {r.reasoning}")
    if any(r.year in OPEN_BOOK_YEARS for r in results.values()):
        years = ", ".join(str(y) for y in sorted(OPEN_BOOK_YEARS))
        print(f"\nNote: {years} exams were open-book — calibrate difficulty accordingly.")


# ---- Default output naming ---------------------------------------------------

# Result JSON files live in this subfolder by default (kept out of the project
# root). The browse tool looks here too. Override the full path with --out.
RESULTS_DIR = Path("results")

COURSE_NAME_RE = re.compile(r"/t-([^./]+)\.html", re.IGNORECASE)


def extract_course_name(url: str) -> str:
    """Pull the course slug from a tripos page URL: t-Cybersecurity.html -> Cybersecurity."""
    m = COURSE_NAME_RE.search(url)
    return m.group(1) if m else "tripos"


def slugify_topic(topic: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return s or "topic"


def default_out_path(url: str, topic: str) -> Path:
    return RESULTS_DIR / f"{extract_course_name(url)}_{slugify_topic(topic)}.json"


def local_out_path(url: str, topic: str) -> Path:
    """Like default_out_path but with a '.local' marker so API and local
    results for the same topic don't collide and are visibly distinct."""
    return RESULTS_DIR / f"{extract_course_name(url)}_{slugify_topic(topic)}.local.json"


# ---- Cumulative usage log ----------------------------------------------------

DEFAULT_USAGE_LOG = Path.home() / ".tripos_topic_usage.json"


def update_usage_log(log_path: Path, entry: dict) -> dict:
    """Append a run entry to the log and recompute aggregates."""
    if log_path.exists():
        try:
            log = json.loads(log_path.read_text())
        except json.JSONDecodeError as e:
            print(
                f"Warning: usage log {log_path} is corrupted ({e}); starting fresh.",
                file=sys.stderr,
            )
            log = {"runs": []}
    else:
        log = {"runs": []}

    log.setdefault("runs", []).append(entry)
    runs = log["runs"]
    log["total_runs"] = len(runs)
    log["total_calls"] = sum(r["calls"] for r in runs)
    log["total_input_tokens"] = sum(r["input_tokens"] for r in runs)
    log["total_output_tokens"] = sum(r["output_tokens"] for r in runs)
    log["total_cost_usd"] = sum(r["cost_usd"] for r in runs)
    log["updated_at"] = datetime.now(timezone.utc).isoformat()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(log, indent=2))
    return log


# ---- Main --------------------------------------------------------------------

def estimate_cost(n: int) -> float:
    in_cost = n * EST_INPUT_TOKENS_PER_PDF / 1_000_000 * PRICE_IN_PER_MTOK
    out_cost = n * EST_OUTPUT_TOKENS_PER_CALL / 1_000_000 * PRICE_OUT_PER_MTOK
    return in_cost + out_cost


def run_local(args: argparse.Namespace, all_papers: list[Paper], latest: int) -> int:
    """Score questions by local fuzzy text search instead of the Claude API."""
    if PdfReader is None:
        print("Local search needs pypdf: pip install pypdf", file=sys.stderr)
        return 2

    cache_dir = Path(args.cache)
    results_path = Path(args.out) if args.out else local_out_path(args.url, args.topic)
    print(f"Local fuzzy search (no API cost) → {results_path}")

    prev_topic, prior_results = load_existing(results_path)
    if prior_results and prev_topic != args.topic:
        print(f"Results file exists for a different topic ('{prev_topic}'); will overwrite.")
        existing: dict[str, Result] = {}
    elif prior_results:
        print(f"Resuming: {len(prior_results)} questions already searched for this topic.")
        existing = dict(prior_results)
    else:
        existing = {}

    papers = window_by_years(all_papers, latest)
    win_years = unique_years(papers)
    if len(papers) < len(all_papers):
        print(f"Window: {len(papers)} questions from the {len(win_years)} most "
              f"recent years ({fmt_years(win_years)}) of {len(all_papers)} total.")
    else:
        print(f"Window: all {len(all_papers)} questions ({fmt_years(win_years)}).")

    todo = [p for p in papers if p.paper_id not in existing]
    if not todo:
        print("Nothing to do in this window.")
        print_summary(existing)
        return 0

    print(f"Searching {len(todo)} PDFs locally for {args.topic!r}...")
    results = dict(existing)
    for paper in todo:
        try:
            pdf_path = ensure_pdf_cached(paper, cache_dir)
            text = extract_pdf_text(pdf_path)
        except Exception as e:
            print(f"  ✗ {paper.paper_id}: {e}", file=sys.stderr)
            continue
        score, reasoning = local_score(text, args.topic)
        ob = " [open-book]" if paper.year in OPEN_BOOK_YEARS else ""
        pid = link(paper.paper_id, paper.pdf_url)
        print(f"  {score} {pid}{ob} — {reasoning[:80]}{'…' if len(reasoning) > 80 else ''}")
        results[paper.paper_id] = Result(
            paper_id=paper.paper_id,
            year=paper.year,
            paper=paper.paper,
            question=paper.question,
            pdf_url=paper.pdf_url,
            file_id="",  # no Files API upload in local mode
            score=score,
            reasoning=reasoning,
        )
        write_results(results_path, args.topic, args.url, LOCAL_MODEL, results)

    write_results(results_path, args.topic, args.url, LOCAL_MODEL, results)
    print_summary(results)
    print(f"\nLocal search complete (no API cost). Results written to {results_path}")
    return 0


async def run(args: argparse.Namespace) -> int:
    cache_dir = Path(args.cache)
    manifest_path = Path(args.manifest)

    print(f"Scraping {args.url}...")
    papers = scrape_links(args.url)
    if not papers:
        print("Found 0 question links.")
        return 1
    years = unique_years(papers)
    print(f"Found {len(papers)} question links across {len(years)} years: {fmt_years(years)}.")

    all_papers = papers

    # --local skips the API entirely and goes straight to local fuzzy search.
    if args.local:
        return run_local(args, all_papers, args.latest)

    results_path = Path(args.out) if args.out else default_out_path(args.url, args.topic)
    print(f"Output: {results_path}")
    prev_topic, prior_results = load_existing(results_path)
    if prior_results and prev_topic != args.topic:
        print(f"Results file exists for a different topic ('{prev_topic}'); will overwrite.")
        existing: dict[str, Result] = {}
    elif prior_results:
        print(f"Resuming: {len(prior_results)} questions already analysed for this topic.")
        existing = dict(prior_results)
    else:
        existing = {}

    # Cost confirmation loop: y runs the API analysis, n (the default) runs a
    # free local fuzzy search instead, a number re-caps to that many most-recent
    # years and re-prompts, q aborts.
    current_latest = args.latest
    while True:
        papers = window_by_years(all_papers, current_latest)
        win_years = unique_years(papers)
        if len(papers) < len(all_papers):
            print(f"Window: {len(papers)} questions from the {len(win_years)} most "
                  f"recent years ({fmt_years(win_years)}) of {len(all_papers)} total.")
        else:
            print(f"Window: all {len(all_papers)} questions ({fmt_years(win_years)}).")

        todo = [p for p in papers if p.paper_id not in existing]

        if not todo:
            print("Nothing to do in this window.")
            print_summary(existing)
            return 0

        cost = estimate_cost(len(todo))
        print(f"Estimated cost: ~${cost:.2f} for {len(todo)} calls (model={args.model})")

        if args.yes:
            break

        ans = input(
            "Proceed with API analysis? [y / N=local fuzzy search], "
            "a number to cap to the N most recent years, or q to abort: "
        ).strip().lower()
        if ans in ("y", "yes"):
            break
        if ans in ("", "n", "no"):
            return run_local(args, all_papers, current_latest)
        if ans.isdigit() and int(ans) > 0:
            current_latest = int(ans)
            continue
        if ans in ("q", "quit"):
            return 1
        # Unrecognised input: re-prompt rather than silently aborting.

    sync_client = anthropic.Anthropic()
    async_client = anthropic.AsyncAnthropic()
    manifest = load_manifest(manifest_path)

    # Sync phase: download PDFs and upload to Files API
    print("Caching PDFs and ensuring uploads...")
    file_ids: dict[str, str] = {}
    for paper in todo:
        try:
            pdf_path = ensure_pdf_cached(paper, cache_dir)
            file_id = ensure_uploaded(sync_client, paper, pdf_path, manifest, manifest_path)
            file_ids[paper.paper_id] = file_id
        except Exception as e:
            print(f"  ✗ {paper.paper_id}: {e}", file=sys.stderr)

    # Async phase: analyse
    print(f"Analysing {len(file_ids)} questions (concurrency={args.concurrency})...")
    semaphore = asyncio.Semaphore(args.concurrency)
    usage = RunUsage()

    results = dict(existing)

    async def run_one(paper: Paper) -> None:
        r = await analyse(
            async_client, paper, file_ids[paper.paper_id],
            args.topic, args.model, semaphore, usage,
        )
        if r is not None:
            results[r.paper_id] = r
            # Persist incrementally so a crash doesn't lose work
            write_results(results_path, args.topic, args.url, args.model, results)

    await asyncio.gather(*(run_one(p) for p in todo if p.paper_id in file_ids))

    write_results(results_path, args.topic, args.url, args.model, results)
    print_summary(results)
    print(
        f"\nUsage: {usage.calls} calls, "
        f"{usage.input_tokens:,} in + {usage.output_tokens:,} out tokens"
    )
    print(
        f"Actual cost: ${usage.cost_usd():.4f}  "
        f"(at ${PRICE_IN_PER_MTOK}/Mtok in, ${PRICE_OUT_PER_MTOK}/Mtok out)"
    )

    if usage.calls > 0:
        log_path = Path(args.usage_log).expanduser()
        log = update_usage_log(log_path, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "course": extract_course_name(args.url),
            "topic": args.topic,
            "page_url": args.url,
            "model": args.model,
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": usage.cost_usd(),
        })
        print(
            f"Cumulative across {log['total_runs']} runs: "
            f"${log['total_cost_usd']:.4f}  "
            f"({log['total_calls']:,} calls, log: {log_path})"
        )

    print(f"Results written to {results_path}")
    return 0


def main() -> int:
    load_dotenv()  # picks up ./.env if present; silently does nothing otherwise

    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("course", nargs="?",
                   help="Course name (e.g. 'Logic and Proof'), a substring, an "
                        "abbreviation ('concdist'), or full URL. Fuzzy-matched.")
    p.add_argument("topic", nargs="?", help="The topic to score relevance against.")
    p.add_argument("--list-courses", action="store_true",
                   help="Print the hardcoded list of courses and exit.")
    p.add_argument("--out", default=None,
                   help="Output JSON path (default: <CourseName>_<topic-slug>.json)")
    p.add_argument("--cache", default="./pdf_cache", help="Local PDF cache dir (default: ./pdf_cache)")
    p.add_argument("--manifest", default="./files_manifest.json",
                   help="Files API manifest (default: ./files_manifest.json)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default: {DEFAULT_MODEL})")
    p.add_argument("--concurrency", type=int, default=5, help="Concurrent API calls (default: 5)")
    p.add_argument("--latest", type=int, default=0, metavar="N",
                   help="Only analyse questions from the N most recent years (default: all)")
    p.add_argument("-y", "--yes", action="store_true", help="Skip cost confirmation prompt")
    p.add_argument("--local", action="store_true",
                   help="Skip the Claude API: score by local fuzzy text search of the "
                        "PDFs instead (free, no API key). Output gets a '.local' marker.")
    p.add_argument("--usage-log", default=str(DEFAULT_USAGE_LOG),
                   help=f"Cumulative cost log (default: {DEFAULT_USAGE_LOG})")
    args = p.parse_args()

    if args.list_courses:
        list_courses()
        return 0

    if not args.course or not args.topic:
        p.error("course and topic are required (or pass --list-courses)")

    try:
        args.url = resolve_course(args.course)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    # Local mode needs no API key; interactive mode may still fall back to it,
    # but if you have no key you should pass --local explicitly.
    if not args.local and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY env var not set (or use --local for offline search).",
              file=sys.stderr)
        return 2

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
