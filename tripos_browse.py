#!/usr/bin/env python3
"""tripos_browse.py — TUI to browse saved tripos topic-relevance JSON files.

Run from any directory containing the JSON files produced by tripos_topic.py.

Keys:
    ↑ ↓ / click   navigate
    /             focus question search (filters questions by paper ID + reasoning)
    f             focus file search (filters the file list by name)
    Esc           clear the focused search / blur input
    0 1 2 3       filter table by minimum score (0 = all)
    o             open the highlighted question's PDF in your browser
    O (shift+o)   open every PDF currently shown in the table
    r             refresh file list
    q             quit

A 📖 marker flags questions from open-book exam years (2020–2022).
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def open_url(url: str) -> None:
    """Open a URL in the browser without disturbing the TUI's terminal.

    A bare webbrowser.open() lets the launcher (xdg-open / wslview / …) inherit
    our controlling tty, which prints "tcgetpgrp failed: not a tty" job-control
    noise into the TUI. Launch it detached with std streams silenced instead.
    """
    opener = next(
        (c for c in ("wslview", "xdg-open", "open", "cygstart") if shutil.which(c)),
        None,
    )
    if opener:
        try:
            subprocess.Popen(
                [opener, url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # detach from the controlling terminal
            )
            return
        except OSError:
            pass  # fall back to webbrowser below
    # Fallback: silence fds 1/2 so any subprocess webbrowser spawns stays quiet.
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = (os.dup(1), os.dup(2))
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        webbrowser.open(url)
    finally:
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in (devnull, *saved):
            os.close(fd)

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Input, ListItem, ListView, Static


# Years the Tripos exams were sat open-book (COVID-era remote exams).
OPEN_BOOK_YEARS = {2020, 2021, 2022}


# ---- Data --------------------------------------------------------------------

@dataclass
class ResultsFile:
    path: Path
    topic: str
    page_url: str
    model: str
    generated_at: str
    results: list[dict]

    @classmethod
    def load(cls, path: Path) -> "ResultsFile":
        data = json.loads(path.read_text())
        return cls(
            path=path,
            topic=data.get("topic", "?"),
            page_url=data.get("page_url", ""),
            model=data.get("model", "?"),
            generated_at=data.get("generated_at", ""),
            results=data.get("results", []),
        )

    def score_counts(self) -> dict[int, int]:
        c = {0: 0, 1: 0, 2: 0, 3: 0}
        for r in self.results:
            c[r.get("score", 0)] = c.get(r.get("score", 0), 0) + 1
        return c


# tripos_topic.py writes result JSON here by default; fall back to the current
# directory so older flat layouts (and explicit --out paths) still browse.
RESULTS_DIR = Path("results")


def results_dir() -> Path:
    return RESULTS_DIR if RESULTS_DIR.is_dir() else Path(".")


def discover_results_files(directory: Path) -> list[Path]:
    """Find JSON files that look like outputs from tripos_topic.py."""
    out: list[Path] = []
    for p in sorted(directory.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if (
            isinstance(data, dict)
            and isinstance(data.get("topic"), str)
            and isinstance(data.get("results"), list)
            and "model" in data
        ):
            out.append(p)
    return out


def _is_subsequence(needle: str, haystack: str) -> bool:
    """True if every char of needle appears in haystack in order (gaps allowed)."""
    it = iter(haystack)
    return all(c in it for c in needle)


def fuzzy_filter_files(paths: list[Path], query: str) -> list[Path]:
    """Filter result files by name, fuzzily.

    Tiers, first non-empty wins:
      1. Case-insensitive substring (primary, predictable) — original order.
      2. Subsequence on alphanumerics (abbreviations: 'logherb' →
         LogicandProof_herbrand), ranked by similarity.
      3. Closest stems by difflib ratio above a threshold (typos).
    """
    q = query.lower().strip()
    if not q:
        return list(paths)

    substr = [p for p in paths if q in p.stem.lower()]
    if substr:
        return substr

    qn = re.sub(r"[^a-z0-9]", "", q)
    if not qn:
        return []
    norm = {p: re.sub(r"[^a-z0-9]", "", p.stem.lower()) for p in paths}

    def ratio(p: Path) -> float:
        return difflib.SequenceMatcher(None, qn, norm[p]).ratio()

    subseq = [p for p in paths if _is_subsequence(qn, norm[p])]
    if subseq:
        return sorted(subseq, key=ratio, reverse=True)

    ranked = sorted(paths, key=ratio, reverse=True)
    return [p for p in ranked if ratio(p) >= 0.5]


# Cumulative cost log written by tripos_topic.py.
DEFAULT_USAGE_LOG = Path.home() / ".tripos_topic_usage.json"


def usage_summary(log_path: Path = DEFAULT_USAGE_LOG) -> str:
    """One-line cumulative-spend summary for the header, or '' if unavailable."""
    try:
        log = json.loads(log_path.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    runs = log.get("total_runs", len(log.get("runs", [])))
    cost = log.get("total_cost_usd")
    tokens = (log.get("total_input_tokens", 0) or 0) + (log.get("total_output_tokens", 0) or 0)
    if cost is None:
        return ""
    return f"total API spend ${cost:,.2f} · {runs} runs · {tokens / 1000:,.0f}k tokens"


# ---- App ---------------------------------------------------------------------

class BrowseApp(App):
    # Open with the file-search bar focused so you can filter runs by name
    # straight away. Picking a run then jumps focus to the table (see
    # on_list_view_selected) so the 0/1/2/3 priority filters work there.
    AUTO_FOCUS = "#file-search"

    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #sidebar { width: 32; }
    #file-search {
        height: 3;
        border: round $primary;
        margin: 0;
    }
    #file-search:focus {
        border: round $accent;
    }
    #files {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    #main { width: 1fr; }
    #meta {
        height: auto;
        padding: 1 1;
        border: round $primary;
    }
    #table {
        height: 1fr;
        border: round $primary;
    }
    #search {
        height: 3;
        border: round $primary;
        margin: 0;
    }
    #search:focus {
        border: round $accent;
    }
    #reasoning {
        height: 10;
        border: round $primary;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("o", "open_pdf", "Open PDF"),
        Binding("O", "open_all_pdfs", "Open all"),
        Binding("slash", "focus_search", "Search"),
        Binding("f", "focus_file_search", "Find file"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("0", "set_min_score(0)", "All"),
        Binding("1", "set_min_score(1)", "≥1"),
        Binding("2", "set_min_score(2)", "≥2"),
        Binding("3", "set_min_score(3)", "=3"),
    ]

    current_file: reactive[Optional[ResultsFile]] = reactive(None)
    min_score: reactive[int] = reactive(0)
    search_query: reactive[str] = reactive("")
    file_search_query: reactive[str] = reactive("")

    def __init__(self) -> None:
        super().__init__()
        self._file_paths: list[Path] = []
        self._visible_file_paths: list[Path] = []
        self._visible_rows: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Input(placeholder="Filter files… (f to focus)", id="file-search")
                yield ListView(id="files")
            with Vertical(id="main"):
                yield Static(id="meta")
                yield Input(placeholder="Search paper ID or reasoning… (/ to focus, Esc to clear)", id="search")
                yield DataTable(id="table")
                yield Static(id="reasoning")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Tripos Topic Browser"
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Score", "Paper", "Reasoning")
        self.action_refresh()

    # ---- Loading ----

    def action_refresh(self) -> None:
        self.sub_title = usage_summary() or "no usage log found"
        self._file_paths = discover_results_files(results_dir())
        if not self._file_paths:
            self.query_one("#files", ListView).clear()
            self._visible_file_paths = []
            self.current_file = None
            self.query_one("#meta", Static).update(
                "[red]No tripos results JSON files in this directory.[/red]\n"
                "Run [b]tripos_topic.py[/b] first, then come back."
            )
            return
        self._refresh_file_list()

    def _refresh_file_list(self) -> None:
        """Repopulate the sidebar from self._file_paths, applying the file filter."""
        files_view = self.query_one("#files", ListView)
        files_view.clear()
        self._visible_file_paths = fuzzy_filter_files(
            self._file_paths, self.file_search_query
        )
        for p in self._visible_file_paths:
            files_view.append(ListItem(Static(p.stem)))
        if self._visible_file_paths:
            files_view.index = 0
            self._load_file(self._visible_file_paths[0])
        else:
            self.current_file = None
            self.query_one("#meta", Static).update(
                f"[dim](no files match {self.file_search_query!r})[/dim]"
            )

    def _load_file(self, path: Path) -> None:
        try:
            self.current_file = ResultsFile.load(path)
        except Exception as e:
            self.query_one("#meta", Static).update(f"[red]Failed to load {path}: {e}[/red]")

    # ---- Reactivity ----

    def watch_current_file(self, _rf: Optional[ResultsFile]) -> None:
        self._refresh_meta()
        self._refresh_table()

    def watch_min_score(self, _v: int) -> None:
        self._refresh_meta()
        self._refresh_table()

    def watch_search_query(self, _v: str) -> None:
        self._refresh_meta()
        self._refresh_table()

    def watch_file_search_query(self, _v: str) -> None:
        # Skip during initial construction, before the file list exists.
        if self._file_paths:
            self._refresh_file_list()

    def _refresh_meta(self) -> None:
        rf = self.current_file
        if rf is None:
            return
        c = rf.score_counts()
        total = sum(c.values())
        notes = []
        if self.min_score > 0:
            notes.append(f"score ≥ {self.min_score}")
        if self.search_query:
            notes.append(f"search: {self.search_query!r}")
        filt_note = f"  [dim](filtering: {', '.join(notes)})[/dim]" if notes else ""
        n_ob = sum(1 for r in rf.results if r.get("year") in OPEN_BOOK_YEARS)
        ob_note = f"   [yellow]📖 {n_ob} open-book[/yellow]" if n_ob else ""
        lines = [
            f"[b]Topic:[/b]  {rf.topic}{filt_note}",
            f"[b]Page:[/b]   [dim]{rf.page_url}[/dim]",
            f"[b]Model:[/b]  {rf.model}    [b]Generated:[/b] {rf.generated_at}",
            f"[b]Counts:[/b] 3 → {c[3]}   2 → {c[2]}   1 → {c[1]}   0 → {c[0]}   (total {total}){ob_note}",
        ]
        self.query_one("#meta", Static).update("\n".join(lines))

    def _refresh_table(self) -> None:
        rf = self.current_file
        table = self.query_one("#table", DataTable)
        table.clear()
        if rf is None:
            self._visible_rows = []
            return
        rows = [r for r in rf.results if r.get("score", 0) >= self.min_score]
        if self.search_query:
            q = self.search_query.lower()
            rows = [
                r for r in rows
                if q in r["paper_id"].lower() or q in r.get("reasoning", "").lower()
            ]
        rows.sort(key=lambda r: (-r["score"], r["year"], r["paper"], r["question"]))
        self._visible_rows = rows
        for r in rows:
            short = r.get("reasoning", "")
            if len(short) > 100:
                short = short[:97] + "..."
            paper = r["paper_id"]
            if r.get("year") in OPEN_BOOK_YEARS:
                paper += " 📖"
            table.add_row(str(r["score"]), paper, short, key=r["paper_id"])
        if rows:
            self._show_reasoning(rows[0])
        else:
            self.query_one("#reasoning", Static).update("[dim](no questions match filter)[/dim]")

    def _show_reasoning(self, row: dict) -> None:
        ob = (
            "   [black on yellow] 📖 OPEN-BOOK EXAM [/]"
            if row.get("year") in OPEN_BOOK_YEARS
            else ""
        )
        text = (
            f"[b]{row['paper_id']}[/b]   score [b]{row['score']}[/b]{ob}\n"
            f"{row.get('reasoning', '')}\n\n"
            f"[dim]{row.get('pdf_url', '')}[/dim]"
        )
        self.query_one("#reasoning", Static).update(text)

    # ---- Events ----

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = self.query_one("#files", ListView).index
        if idx is not None and 0 <= idx < len(self._visible_file_paths):
            self._load_file(self._visible_file_paths[idx])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Picking a run (Enter / click) jumps to the table so priority
        # filters and navigation work without a detour through search.
        self.query_one("#table", DataTable).focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        key = event.row_key.value
        row = next((r for r in self._visible_rows if r["paper_id"] == key), None)
        if row:
            self._show_reasoning(row)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.search_query = event.value
        elif event.input.id == "file-search":
            self.file_search_query = event.value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter moves focus onward for keyboard navigation.
        if event.input.id == "search":
            self.query_one("#table", DataTable).focus()
        elif event.input.id == "file-search":
            # Highlight the first matching run so ↑/↓ can pick from the results.
            files = self.query_one("#files", ListView)
            if len(files):
                files.index = 0
            files.focus()

    # ---- Actions ----

    def action_set_min_score(self, score: int) -> None:
        self.min_score = score

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_focus_file_search(self) -> None:
        self.query_one("#file-search", Input).focus()

    def action_clear_search(self) -> None:
        file_inp = self.query_one("#file-search", Input)
        q_inp = self.query_one("#search", Input)
        # Clear whichever search owns focus; otherwise clear any non-empty box.
        if file_inp.has_focus:
            file_inp.value = ""
            self.query_one("#files", ListView).focus()
        elif q_inp.has_focus:
            q_inp.value = ""
            self.query_one("#table", DataTable).focus()
        elif q_inp.value:
            q_inp.value = ""
        elif file_inp.value:
            file_inp.value = ""

    def action_open_pdf(self) -> None:
        if not self._visible_rows:
            return
        table = self.query_one("#table", DataTable)
        if table.cursor_row < 0 or table.cursor_row >= len(self._visible_rows):
            return
        row = self._visible_rows[table.cursor_row]
        url = row.get("pdf_url")
        if url:
            open_url(url)

    def action_open_all_pdfs(self) -> None:
        """Open every PDF currently shown in the table (respects score + search filters)."""
        if not self._visible_rows:
            self.notify("No questions shown to open.", severity="warning")
            return
        urls = [u for u in (r.get("pdf_url") for r in self._visible_rows) if u]
        if not urls:
            self.notify("None of the shown questions have a PDF URL.", severity="warning")
            return
        for url in urls:
            open_url(url)
        skipped = len(self._visible_rows) - len(urls)
        msg = f"Opened {len(urls)} PDF{'s' if len(urls) != 1 else ''}."
        if skipped:
            msg += f" ({skipped} skipped — no URL.)"
        self.notify(msg)


def main() -> None:
    BrowseApp().run()


if __name__ == "__main__":
    main()
