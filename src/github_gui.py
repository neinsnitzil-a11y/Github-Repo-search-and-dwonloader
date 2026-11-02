"""Cross-platform, dependency-free Tk desktop application for GitHub ZIP downloads."""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from github_core import (
    GitHubAPI, GitHubError, DownloadCancelled, Link, archive_path,
    download_archive, parse_bulk, save_manifest,
)

BASE = Path(__file__).resolve().parent.parent
BG, PANEL, PANEL2, EDGE = "#0b111b", "#121d2a", "#182638", "#28384b"
FG, MUTED, ACCENT, BLUE, RED = "#eaf4ff", "#99b0c5", "#2de0b0", "#6aafff", "#ff8484"
FONT = ("Segoe UI", 10)


@dataclass
class Job:
    number: int
    link: Link
    name: str
    status: str = "Queued"
    downloaded: int = 0
    total: int = 0
    speed: float = 0
    size: int = 0
    path: str = ""
    error: str = ""


def human(n):
    if not n:
        return "—"
    n = float(n)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {suffix}" if suffix != "B" else f"{n:.0f} B"
        n /= 1024
    return f"{n:.1f} PB"


def button(parent, text, command, prominent=False, **kwargs):
    return tk.Button(parent, text=text, command=command, bg=ACCENT if prominent else PANEL2,
                     fg="#091720" if prominent else FG, activebackground=BLUE,
                     activeforeground="#091720", bd=0, relief="flat", padx=14, pady=8,
                     font=("Segoe UI Semibold", 10), cursor="hand2", **kwargs)


def title(parent, heading, sub):
    tk.Label(parent, text=heading, bg=BG, fg=FG, font=("Segoe UI Semibold", 17)).pack(anchor="w", pady=(8, 1))
    tk.Label(parent, text=sub, bg=BG, fg=MUTED, font=FONT).pack(anchor="w", pady=(0, 14))


class Studio(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("GitHub Archive Studio  •  Search, Scrape & ZIP Downloads")
        self.geometry("1260x830")
        self.minsize(1020, 700)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.events = queue.Queue()
        self.results = []
        self.search_page = 1
        self.search_serial = 0
        self.jobs: dict[int, Job] = {}
        self.job_keys = set()
        self.counter = 0
        self.running = False
        self.pause_event = threading.Event()
        self.stop_event = threading.Event()
        self.work_lock = threading.Lock()
        self.destination = tk.StringVar(value=str(Path.home() / "Downloads" / "GitHub_Archive_Studio"))
        self.token = tk.StringVar(value=os.environ.get("GITHUB_TOKEN", ""))
        self.search_term = tk.StringVar()
        self.language = tk.StringVar()
        self.sort = tk.StringVar(value="Best match")
        self.concurrent = tk.IntVar(value=3)
        self.status = tk.StringVar(value="Ready. Search GitHub or import a TXT link list.")
        self.search_info = tk.StringVar(value="Search GitHub's public repository index")
        self.total_info = tk.StringVar(value="0 jobs queued")
        self.setup_styles()
        self.draw()
        self.after(120, self.poll)

    def setup_styles(self):
        sty = ttk.Style(self)
        sty.theme_use("clam")
        sty.configure("TNotebook", background=BG, borderwidth=0)
        sty.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, font=("Segoe UI Semibold", 10), padding=(20, 12), borderwidth=0)
        sty.map("TNotebook.Tab", background=[("selected", PANEL2)], foreground=[("selected", ACCENT)])
        sty.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                      rowheight=29, borderwidth=0, font=FONT)
        sty.map("Treeview", background=[("selected", "#245473")], foreground=[("selected", "#ffffff")])
        sty.configure("Treeview.Heading", background=PANEL2, foreground=FG,
                      font=("Segoe UI Semibold", 10), padding=8, relief="flat")
        sty.configure("Horizontal.TProgressbar", troughcolor=PANEL2, background=ACCENT,
                      bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT)
        sty.configure("TCombobox", foreground="#071423", fieldbackground="#e6f6fc", padding=4)
        sty.configure("TSpinbox", foreground="#071423", fieldbackground="#e6f6fc")

    def frame(self, parent):
        return tk.Frame(parent, bg=BG)

    def draw(self):
        banner = tk.Frame(self, bg=PANEL, height=90)
        banner.pack(fill="x")
        tk.Label(banner, text="⌁  GitHub Archive Studio", bg=PANEL, fg=ACCENT,
                 font=("Segoe UI Semibold", 23)).pack(side="left", padx=24, pady=20)
        tk.Label(banner, text="REPOSITORY SEARCH   •   BULK TXT IMPORT   •   ZIP QUEUE", bg=PANEL, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="right", padx=22)
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=20, pady=(16, 10))
        self.page_search = self.frame(self.tabs)
        self.page_import = self.frame(self.tabs)
        self.page_queue = self.frame(self.tabs)
        self.page_settings = self.frame(self.tabs)
        self.tabs.add(self.page_search, text="   01  SEARCH   ")
        self.tabs.add(self.page_import, text="   02  MASS TXT LINKS   ")
        self.tabs.add(self.page_queue, text="   03  DOWNLOAD QUEUE   ")
        self.tabs.add(self.page_settings, text="   04  GITHUB ACCESS   ")
        self.draw_search()
        self.draw_import()
        self.draw_queue()
        self.draw_settings()
        bottom = tk.Frame(self, bg=PANEL)
        bottom.pack(fill="x")
        tk.Label(bottom, textvariable=self.status, bg=PANEL, fg=MUTED, anchor="w", font=FONT).pack(side="left", padx=20, pady=9)
        tk.Label(bottom, text="v1.0 • Python/Tkinter", bg=PANEL, fg=MUTED).pack(side="right", padx=22)

    def draw_search(self):
        page = self.page_search
        title(page, "Explore GitHub repositories", "Search by keyword, language, stars, topic or owner. Select a result to add its source ZIP to the download queue.")
        top = tk.Frame(page, bg=BG)
        top.pack(fill="x", pady=(0, 10))
        tk.Entry(top, textvariable=self.search_term, font=("Segoe UI", 13), bg=PANEL2,
                 fg=FG, insertbackground=FG, bd=0, relief="flat").pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 10))
        button(top, "SEARCH REPOSITORIES", self.search, prominent=True).pack(side="left")
        self.search_term.set("python tools")
        self.bind("<Return>", lambda ev: self.search() if self.tabs.index("current") == 0 else None)
        filters = tk.Frame(page, bg=BG)
        filters.pack(fill="x", pady=(0, 12))
        tk.Label(filters, text="Language", bg=BG, fg=MUTED).pack(side="left", padx=(0, 6))
        tk.Entry(filters, textvariable=self.language, width=18, bg=PANEL2, fg=FG,
                 insertbackground=FG, bd=0, font=FONT).pack(side="left", ipady=5, padx=(0, 20))
        tk.Label(filters, text="Sort", bg=BG, fg=MUTED).pack(side="left", padx=(0, 6))
        ttk.Combobox(filters, textvariable=self.sort, state="readonly", width=17,
                     values=("Best match", "Most stars", "Recently updated", "Most forks")).pack(side="left", padx=(0, 16))
        tk.Label(filters, textvariable=self.search_info, bg=BG, fg=MUTED).pack(side="right")
        area = tk.Frame(page, bg=PANEL)
        area.pack(fill="both", expand=True)
        cols = ("repo", "stars", "lang", "updated", "desc")
        self.search_tree = ttk.Treeview(area, columns=cols, show="headings", selectmode="extended")
        for col, label, width in (("repo", "OWNER / REPOSITORY", 230), ("stars", "STARS", 85),
                                  ("lang", "LANGUAGE", 120), ("updated", "UPDATED", 108),
                                  ("desc", "DESCRIPTION", 520)):
            self.search_tree.heading(col, text=label)
            self.search_tree.column(col, width=width, minwidth=70, stretch=col in ("repo", "desc"))
        ys = ttk.Scrollbar(area, command=self.search_tree.yview)
        self.search_tree.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.search_tree.pack(fill="both", expand=True)
        self.search_tree.bind("<Double-1>", lambda e: self.add_selected_results())
        actions = tk.Frame(page, bg=BG)
        actions.pack(fill="x", pady=12)
        button(actions, "+ QUEUE SELECTED ZIPS", self.add_selected_results, prominent=True).pack(side="left", padx=(0, 8))
        button(actions, "+ Queue all on page", self.add_all_results).pack(side="left", padx=(0, 8))
        button(actions, "Open on GitHub", self.open_selected_result).pack(side="left")
        button(actions, "Next →", lambda: self.change_page(1)).pack(side="right")
        button(actions, "← Previous", lambda: self.change_page(-1)).pack(side="right", padx=8)

    def draw_import(self):
        page = self.page_import
        title(page, "Import large GitHub link lists", "Paste URLs or load any .txt file. GitHub repository, profile, archive and direct HTTPS ZIP links are recognized.")
        row = tk.Frame(page, bg=BG)
        row.pack(fill="x", pady=(0, 8))
        button(row, "LOAD TXT FILE", self.load_txt, prominent=True).pack(side="left", padx=(0, 8))
        button(row, "Load bundled GitHub list", self.load_example).pack(side="left", padx=(0, 8))
        button(row, "Clear text", lambda: self.bulk_text.delete("1.0", "end")).pack(side="left")
        self.import_info = tk.StringVar(value="Import list includes automatic duplicate detection")
        tk.Label(row, textvariable=self.import_info, fg=MUTED, bg=BG).pack(side="right")
        self.bulk_text = ScrolledText(page, height=19, wrap="word", bg=PANEL, fg=FG,
                                      insertbackground=ACCENT, selectbackground="#2a526f",
                                      font=("Consolas", 10), bd=0, padx=14, pady=14)
        self.bulk_text.pack(fill="both", expand=True)
        self.bulk_text.insert("1.0", "# Paste your GitHub repository links here (one per line or any text)\n"
                                    "# Examples:\n# https://github.com/python/cpython\n# psf/requests\n"
                                    "# https://github.com/owner/repo/archive/refs/heads/main.zip\n")
        controls = tk.Frame(page, bg=BG)
        controls.pack(fill="x", pady=12)
        button(controls, "+ IMPORT LINKS TO QUEUE", self.import_text, prominent=True).pack(side="left", padx=(0, 10))
        button(controls, "Import and start downloads", lambda: self.import_text(start=True)).pack(side="left", padx=(0, 10))
        tk.Label(controls, text="GitHub profile URLs expand to public repositories using the API (up to 500/profile).",
                 bg=BG, fg=MUTED).pack(side="right")

    def draw_queue(self):
        page = self.page_queue
        title(page, "Download manager", "Every ZIP goes into its own owner/repository folder. Existing valid archives are skipped; partial downloads resume where supported.")
        destrow = tk.Frame(page, bg=BG)
        destrow.pack(fill="x", pady=(0, 12))
        tk.Label(destrow, text="SAVE TO", fg=ACCENT, bg=BG, font=("Segoe UI Semibold", 10)).pack(side="left", padx=(0, 10))
        tk.Entry(destrow, textvariable=self.destination, bg=PANEL2, fg=FG, insertbackground=FG,
                 font=FONT, bd=0).pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 7))
        button(destrow, "Browse", self.pick_folder).pack(side="left", padx=(0, 6))
        button(destrow, "Open Downloads", self.open_downloads).pack(side="left")
        cmds = tk.Frame(page, bg=BG)
        cmds.pack(fill="x", pady=(0, 12))
        button(cmds, "▶ START QUEUE", self.start, prominent=True).pack(side="left", padx=(0, 7))
        button(cmds, "Pause / Resume", self.toggle_pause).pack(side="left", padx=(0, 7))
        button(cmds, "Stop", self.stop).pack(side="left", padx=(0, 7))
        button(cmds, "Retry failed", self.retry_failed).pack(side="left", padx=(0, 7))
        button(cmds, "Remove selected", self.remove_selected).pack(side="left", padx=(0, 7))
        button(cmds, "Export report", self.export_report).pack(side="left")
        tk.Label(cmds, text="Parallel", bg=BG, fg=MUTED).pack(side="right", padx=(6, 3))
        ttk.Spinbox(cmds, from_=1, to=6, textvariable=self.concurrent, width=4).pack(side="right")
        area = tk.Frame(page, bg=PANEL)
        area.pack(fill="both", expand=True)
        cols = ("name", "status", "progress", "speed", "size", "path")
        self.queue_tree = ttk.Treeview(area, columns=cols, show="headings", selectmode="extended")
        for col, label, width in (("name", "REPOSITORY / ARCHIVE", 225), ("status", "STATUS", 133),
                                  ("progress", "PROGRESS", 127), ("speed", "SPEED", 90),
                                  ("size", "SAVED", 85), ("path", "OUTPUT FILE", 440)):
            self.queue_tree.heading(col, text=label)
            self.queue_tree.column(col, width=width, minwidth=75, stretch=col in ("name", "path"))
        self.queue_tree.pack(side="left", fill="both", expand=True)
        queue_scroll = ttk.Scrollbar(area, command=self.queue_tree.yview)
        queue_scroll.pack(side="right", fill="y")
        self.queue_tree.configure(yscrollcommand=queue_scroll.set)
        self.queue_tree.bind("<Double-1>", lambda e: self.open_selected_file())
        summary = tk.Frame(page, bg=BG)
        summary.pack(fill="x", pady=(10, 5))
        tk.Label(summary, textvariable=self.total_info, bg=BG, fg=ACCENT,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        self.progress = ttk.Progressbar(summary, maximum=100, length=300)
        self.progress.pack(side="right")
        tk.Label(page, text="Activity / errors", bg=BG, fg=MUTED).pack(anchor="w")
        self.activity = ScrolledText(page, height=7, bg=PANEL, fg=MUTED, font=("Consolas", 9),
                                     insertbackground=FG, relief="flat", state="disabled")
        self.activity.pack(fill="x", pady=(5, 4))

    def draw_settings(self):
        page = self.page_settings
        title(page, "GitHub API access", "Public repository search and downloads work without signing in. A personal access token raises GitHub API limits.")
        card = tk.Frame(page, bg=PANEL, padx=22, pady=22)
        card.pack(fill="x", pady=(0, 12))
        tk.Label(card, text="OPTIONAL GITHUB PERSONAL ACCESS TOKEN", bg=PANEL, fg=ACCENT,
                 font=("Segoe UI Semibold", 11)).pack(anchor="w")
        tk.Label(card, text="The token stays in this app's memory only; it is not saved to a configuration file.",
                 bg=PANEL, fg=MUTED).pack(anchor="w", pady=(6, 12))
        self.token_entry = tk.Entry(card, textvariable=self.token, show="●", bg=PANEL2,
                                    fg=FG, insertbackground=FG, font=FONT, bd=0)
        self.token_entry.pack(fill="x", ipady=8)
        var = tk.BooleanVar(value=False)
        tk.Checkbutton(card, text="Show token", variable=var, command=lambda: self.token_entry.configure(show="" if var.get() else "●"),
                       bg=PANEL, fg=FG, selectcolor=PANEL2, activebackground=PANEL,
                       activeforeground=FG).pack(anchor="w", pady=(10, 0))
        howto = tk.Frame(page, bg=PANEL, padx=22, pady=20)
        howto.pack(fill="x")
        tk.Label(howto, text="SUPPORTED BULK INPUT", bg=PANEL, fg=ACCENT,
                 font=("Segoe UI Semibold", 11)).pack(anchor="w")
        msg = ("• github.com/owner/repo and owner/repo shorthand → full default-branch source ZIP\n"
               "• github.com/owner → scan the user's public repositories, max 500\n"
               "• GitHub archive / zipball URLs → specific branches / tags when identifiable\n"
               "• GitHub release .zip assets and direct HTTPS .zip URLs → ZIP downloads\n"
               "• Other websites → not crawled; unsupported URLs are listed during import\n\n"
               "GitHub search uses official REST API. It does not bypass rate limits or access controls.\n"
               "Private repos require a token with authorized read permissions.\n"
               "All downloads are saved as archives only; nothing is executed or extracted.")
        tk.Label(howto, text=msg, bg=PANEL, fg=FG, justify="left", font=("Segoe UI", 11)).pack(anchor="w", pady=(12, 0))
        button(page, "Open GitHub token settings", lambda: webbrowser.open("https://github.com/settings/tokens")).pack(anchor="w", pady=14)

    def emit(self, kind, *values):
        self.events.put((kind, *values))

    def log(self, text):
        self.activity.configure(state="normal")
        self.activity.insert("end", time.strftime("%H:%M:%S") + "  " + str(text) + "\n")
        self.activity.see("end")
        self.activity.configure(state="disabled")

    def search(self):
        term = self.search_term.get().strip()
        if not term:
            messagebox.showinfo("Search", "Enter keywords to search GitHub.")
            return
        self.search_serial += 1
        serial, page = self.search_serial, self.search_page
        self.search_info.set(f"Searching page {page}…")
        token, lang, sort = self.token.get(), self.language.get(), self.sort.get()
        def worker():
            try:
                result = GitHubAPI(token).search(term, sort, lang, page=page)
                self.emit("search", serial, page, result)
            except Exception as exc:
                self.emit("search_error", serial, str(exc))
        threading.Thread(target=worker, daemon=True).start()

    def change_page(self, delta):
        next_page = self.search_page + delta
        if next_page < 1 or next_page > 34:
            return
        self.search_page = next_page
        self.search()

    def render_search(self, page, result):
        self.results = result.get("items", [])
        for ident in self.search_tree.get_children():
            self.search_tree.delete(ident)
        for idx, repo in enumerate(self.results):
            self.search_tree.insert("", "end", iid=str(idx), values=(repo.get("full_name", ""),
                                    f"{repo.get('stargazers_count', 0):,}", repo.get("language") or "—",
                                    repo.get("updated_at", "")[:10],
                                    (repo.get("description") or "").replace("\n", " ")[:170]))
        total = result.get("total_count", 0)
        incomplete = " • partial results" if result.get("incomplete_results") else ""
        self.search_info.set(f"{total:,} matches • page {page}{incomplete}")
        self.status.set(f"Found {len(self.results)} repositories on page {page}.")

    def add_selected_results(self):
        indices = [int(x) for x in self.search_tree.selection()]
        if not indices:
            messagebox.showinfo("Select repositories", "Select one or more rows first (Ctrl/Shift for multi-select).")
            return
        self.add_repos([self.results[i] for i in indices])

    def add_all_results(self):
        self.add_repos(self.results)

    def add_repos(self, repositories):
        links = []
        for repo in repositories:
            owner = repo["owner"]["login"]
            links.append(Link("repo", repo["html_url"], owner, repo["name"], label=repo["full_name"]))
        added = self.add_links(links)
        self.status.set(f"Queued {added} repositories (duplicates ignored).")
        self.tabs.select(self.page_queue)

    def open_selected_result(self):
        selection = self.search_tree.selection()
        if selection:
            webbrowser.open(self.results[int(selection[0])]["html_url"])

    def load_txt(self):
        path = filedialog.askopenfilename(title="Import GitHub URLs", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as ex:
            messagebox.showerror("Read failed", str(ex))
            return
        self.bulk_text.delete("1.0", "end")
        self.bulk_text.insert("1.0", content)
        self.import_info.set(f"Loaded: {Path(path).name}")

    def load_example(self):
        path = BASE / "Example_List" / "GITHUB_LINKS_ONLY.txt"
        if not path.exists():
            messagebox.showerror("Missing list", "The bundled example list could not be found.")
            return
        self.bulk_text.delete("1.0", "end")
        self.bulk_text.insert("1.0", path.read_text(encoding="utf-8-sig", errors="replace"))
        self.import_info.set("Loaded your extracted GitHub list")

    def import_text(self, start=False):
        text = self.bulk_text.get("1.0", "end")
        links, rejects = parse_bulk(text)
        profiles = [l for l in links if l.kind == "profile"]
        ordinary = [l for l in links if l.kind != "profile"]
        added = self.add_links(ordinary)
        self.import_info.set(f"{len(links)} unique recognized, {len(rejects)} unsupported URLs, {added} queued")
        if rejects:
            out = Path(self.destination.get()).expanduser()
            out.mkdir(parents=True, exist_ok=True)
            (out / "UNSUPPORTED_LINKS.txt").write_text("\n".join(f"Line {line}: {url}  |  {reason}" for line, url, reason in rejects) + "\n", encoding="utf-8")
        self.log(f"Import: {len(ordinary)} repo/ZIP links, {len(profiles)} GitHub profiles, {len(rejects)} unsupported.")
        self.tabs.select(self.page_queue)
        if profiles:
            self.status.set(f"Expanding {len(profiles)} profile(s) via GitHub API…")
            token = self.token.get()
            def expand():
                for p in profiles:
                    try:
                        repos = GitHubAPI(token).list_profile(p.owner)
                        self.emit("profile", p.owner, repos)
                    except Exception as exc:
                        self.emit("profile_fail", p.owner, str(exc))
                self.emit("profiles_done", start)
            threading.Thread(target=expand, daemon=True).start()
        elif start:
            self.start()

    def add_links(self, links):
        added = 0
        for link in links:
            if link.key in self.job_keys:
                continue
            self.counter += 1
            name = link.label or (f"{link.owner}/{link.repo}" if link.kind == "repo" else link.filename)
            job = Job(self.counter, link, name)
            self.jobs[job.number] = job
            self.job_keys.add(link.key)
            self.queue_tree.insert("", "end", iid=str(job.number), values=(name, "Queued", "—", "—", "—", ""))
            added += 1
        self.update_summary()
        return added

    def pick_folder(self):
        selected = filedialog.askdirectory(title="Select master download folder", initialdir=self.destination.get())
        if selected:
            self.destination.set(selected)
            Path(selected).mkdir(parents=True, exist_ok=True)

    def open_downloads(self):
        path = Path(self.destination.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", str(path)])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", str(path)])

    def open_selected_file(self):
        selection = self.queue_tree.selection()
        if not selection:
            return
        job = self.jobs.get(int(selection[0]))
        if job and job.path and Path(job.path).is_file():
            webbrowser.open(Path(job.path).parent.as_uri())

    def start(self):
        if self.running:
            self.status.set("Downloads already running. Use Pause / Resume if paused.")
            return
        pending = [j for j in self.jobs.values() if j.status in ("Queued", "Stopped", "Failed")]
        if not pending:
            self.status.set("Nothing to download. Search GitHub or import a TXT list first.")
            return
        target = Path(self.destination.get()).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
            if not target.is_dir():
                raise OSError("Not a folder")
        except OSError as ex:
            messagebox.showerror("Output error", f"Cannot create output folder: {ex}")
            return
        self.running = True
        self.stop_event.clear()
        self.pause_event.clear()
        threads = min(6, max(1, int(self.concurrent.get() or 3)))
        token = self.token.get()
        for job in pending:
            job.status, job.error = "Waiting", ""
            self.refresh_job(job)
        self.log(f"Starting {len(pending)} archive downloads with {threads} workers → {target}")
        self.status.set(f"Downloading {len(pending)} archives → {target}")
        def run():
            with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="GithubZip") as executor:
                futures = [executor.submit(self.download_one, job, target, token) for job in pending]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:
                        self.emit("log", f"Internal worker error: {exc}")
            self.emit("run_complete")
        threading.Thread(target=run, daemon=True).start()

    def download_one(self, job, target, token):
        if self.stop_event.is_set():
            self.emit("job_done", job.number, "Stopped", "", 0, "Stopped")
            return
        self.emit("job_state", job.number, "Downloading")
        def progress(downloaded, total, speed):
            self.emit("progress", job.number, downloaded, total, speed)
        try:
            state, dest, total = download_archive(job.link, target, token, progress, self.stop_event, self.pause_event)
            self.emit("job_done", job.number, state, str(dest), total, "")
        except DownloadCancelled as exc:
            self.emit("job_done", job.number, "Stopped", "", 0, str(exc))
        except Exception as exc:
            self.emit("job_done", job.number, "Failed", "", 0, str(exc))

    def toggle_pause(self):
        if not self.running:
            self.status.set("Start downloads before using pause.")
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.log("Downloads resumed.")
        else:
            self.pause_event.set()
            self.log("Pausing active streams; finished writes remain on disk.")

    def stop(self):
        if self.running:
            self.stop_event.set()
            self.pause_event.clear()
            self.log("Stop requested. Partial files retained for later resume.")

    def retry_failed(self):
        if self.running:
            messagebox.showinfo("Downloads running", "Stop or finish the current batch before retrying failed links.")
            return
        count = 0
        for job in self.jobs.values():
            if job.status in ("Failed", "Stopped"):
                job.status = "Queued"
                job.error = ""
                job.downloaded, job.total, job.speed = 0, 0, 0
                self.refresh_job(job)
                count += 1
        self.log(f"Requeued {count} failed / stopped archives.")
        self.update_summary()

    def remove_selected(self):
        if self.running:
            messagebox.showinfo("Downloads running", "Finish or stop the current batch before removing jobs.")
            return
        for ident in self.queue_tree.selection():
            job = self.jobs.pop(int(ident), None)
            if job:
                self.job_keys.discard(job.link.key)
                self.queue_tree.delete(ident)
        self.update_summary()
        self.write_report()

    def export_report(self):
        try:
            path = self.write_report()
            messagebox.showinfo("Exported", f"Reports saved in:\n{path}")
        except OSError as ex:
            messagebox.showerror("Export failed", str(ex))

    def write_report(self):
        out = Path(self.destination.get()).expanduser()
        data = [dict(name=j.name, url=j.link.url, status=j.status, path=j.path,
                     size=j.size, error=j.error) for j in self.jobs.values()]
        save_manifest(out, data)
        return out

    def refresh_job(self, job):
        progress = f"{100 * job.downloaded / job.total:.0f}%" if job.total else human(job.downloaded)
        if job.status == "Failed":
            progress = "ERROR — see log"
        self.queue_tree.item(str(job.number), values=(job.name, job.status,
                             progress, human(job.speed) + "/s" if job.speed else "—",
                             human(job.size), job.path))

    def update_summary(self):
        counts = {}
        for j in self.jobs.values():
            counts[j.status] = counts.get(j.status, 0) + 1
        good = counts.get("Completed", 0) + counts.get("Skipped existing", 0)
        failed = counts.get("Failed", 0)
        total = len(self.jobs)
        self.total_info.set(f"{total} total  •  {good} downloaded / existing  •  {failed} failed  •  {counts.get('Downloading', 0)} active")
        self.progress["value"] = (100 * good / total) if total else 0

    def poll(self):
        for _ in range(240):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind, *payload = event
            if kind == "search":
                serial, page, result = payload
                if serial == self.search_serial:
                    self.render_search(page, result)
            elif kind == "search_error":
                serial, error = payload
                if serial == self.search_serial:
                    self.search_info.set("Search failed")
                    self.status.set("Search failed: " + error)
                    messagebox.showerror("GitHub search", error)
            elif kind == "profile":
                owner, links = payload
                added = self.add_links(links)
                self.log(f"Expanded GitHub profile {owner}: {len(links)} repos, {added} added.")
            elif kind == "profile_fail":
                owner, error = payload
                self.log(f"PROFILE FAILED {owner}: {error}")
                self.status.set(f"Profile {owner} failed: {error}")
                out = Path(self.destination.get()).expanduser()
                out.mkdir(parents=True, exist_ok=True)
                with (out / "FAILED_PROFILES.txt").open("a", encoding="utf-8") as fh:
                    fh.write(f"https://github.com/{owner}  |  {error}\n")
            elif kind == "profiles_done":
                if payload[0]:
                    self.start()
            elif kind == "job_state":
                ident, state = payload
                job = self.jobs.get(ident)
                if job:
                    job.status = state
                    self.refresh_job(job)
            elif kind == "progress":
                ident, done, total, speed = payload
                job = self.jobs.get(ident)
                if job:
                    job.downloaded, job.total, job.speed = done, total, speed
                    self.refresh_job(job)
            elif kind == "job_done":
                ident, state, path, size, error = payload
                job = self.jobs.get(ident)
                if job:
                    job.status, job.path, job.size, job.error = state, path, size, error
                    if size:
                        job.downloaded, job.total = size, size
                    self.refresh_job(job)
                    self.log(f"{state}: {job.name}" + (f" | {error}" if error else ""))
            elif kind == "run_complete":
                self.running = False
                self.write_report()
                self.log("Batch finished. DOWNLOAD_MANIFEST.csv and FAILED_LINKS.txt updated.")
                self.status.set("Batch finished. Open Downloads to see your ZIP archives and reports.")
            elif kind == "log":
                self.log(payload[0])
        self.update_summary()
        self.after(120, self.poll)

    def close(self):
        if self.running:
            if not messagebox.askyesno("Downloads in progress", "Stop active transfers and exit? Partial files will remain for resume."):
                return
            self.stop_event.set()
        try:
            self.write_report()
        except OSError:
            pass
        self.destroy()


if __name__ == "__main__":
    Studio().mainloop()
