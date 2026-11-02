"""GitHub Archive Studio core: GitHub REST search, URL import, safe ZIP streaming."""
from __future__ import annotations

import json
import hashlib
import os
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, unquote
from urllib.request import Request, build_opener, HTTPRedirectHandler

USER_AGENT = "GitHubArchiveStudio/1.0 (desktop)"
API_BASE = "https://api.github.com"
URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
SHORTHAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+(?:\.git)?$")
OWNER_RE = re.compile(r"^[a-z\d](?:[a-z\d-]{0,38})$", re.I)
REPO_RE = re.compile(r"^[a-z\d_.-]+$", re.I)
DROP_END = ".,;)>]}!\"'"


@dataclass(frozen=True)
class Link:
    kind: str  # repo | profile | direct
    url: str
    owner: str = ""
    repo: str = ""
    ref: str = ""
    filename: str = ""
    label: str = ""

    @property
    def key(self):
        if self.kind == "repo":
            return f"repo:{self.owner.lower()}/{self.repo.lower()}:{self.ref.lower()}"
        return f"{self.kind}:{self.url.lower()}"


def filename_safe(name, default="archive", limit=110):
    out = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(name)).strip(" .")[:limit]
    return out or default


def valid_owner_repo(owner, repo):
    return bool(OWNER_RE.fullmatch(owner) and REPO_RE.fullmatch(repo) and repo not in (".", ".."))


def parse_link(raw: str):
    raw = raw.strip().strip('"\'`<>').rstrip(DROP_END)
    if not raw:
        return None
    if SHORTHAND_RE.fullmatch(raw):
        owner, repo = raw.split("/", 1)
        repo = re.sub(r"\.git$", "", repo, flags=re.I)
        if valid_owner_repo(owner, repo):
            return Link("repo", f"https://github.com/{owner}/{repo}", owner, repo, label=f"{owner}/{repo}")
    if not raw.startswith(("https://", "http://")):
        return None
    u = urlsplit(raw)
    host = (u.hostname or "").lower()
    if u.scheme != "https":
        return None
    path = unquote(u.path).strip("/")
    segments = path.split("/") if path else []
    if host in ("github.com", "www.github.com", "api.github.com", "codeload.github.com"):
        if host == "api.github.com":
            if len(segments) < 4 or segments[0] != "repos" or segments[3] != "zipball":
                return None
            owner, repo = segments[1:3]
            ref = "/".join(segments[4:]) if len(segments) > 4 else ""
        elif host == "codeload.github.com":
            if len(segments) < 4 or segments[2] != "zip":
                return None
            owner, repo = segments[0:2]
            ref = "/".join(segments[5:]) if len(segments) > 5 and segments[3] == "refs" and segments[4] in ("heads", "tags") else ("/".join(segments[3:]) if len(segments) > 3 else "")
        else:
            if len(segments) == 1 and OWNER_RE.fullmatch(segments[0]):
                return Link("profile", f"https://github.com/{segments[0]}", owner=segments[0], label=f"Profile: {segments[0]}")
            if len(segments) < 2:
                return None
            owner, repo = segments[:2]
            if repo.endswith(".git"):
                repo = repo[:-4]
            if len(segments) >= 5 and segments[2:4] == ["releases", "download"]:
                if path.lower().endswith(".zip"):
                    return Link("direct", urlunsplit((u.scheme, u.netloc, u.path, u.query, "")), owner, repo, filename=segments[-1], label=f"Release ZIP: {owner}/{repo}")
                return None
            ref = ""
            if len(segments) >= 6 and segments[2:5] == ["archive", "refs", "heads"] and segments[-1].lower().endswith(".zip"):
                ref = "/".join(segments[5:])[:-4]
            elif len(segments) >= 6 and segments[2:5] == ["archive", "refs", "tags"] and segments[-1].lower().endswith(".zip"):
                ref = "/".join(segments[5:])[:-4]
            elif len(segments) >= 4 and segments[2] == "archive" and segments[-1].endswith(".zip"):
                ref = segments[-1][:-4]
        if valid_owner_repo(owner, repo):
            return Link("repo", f"https://github.com/{owner}/{repo}", owner, repo, ref=ref, label=f"{owner}/{repo}")
        return None
    # Optional direct HTTPS archives from mass TXT lists, without spidering unrelated pages.
    if u.path.lower().endswith(".zip"):
        safe_url = urlunsplit((u.scheme, u.netloc, u.path, u.query, ""))
        return Link("direct", safe_url, filename=Path(u.path).name, label=f"Direct ZIP: {host}")
    return None


def parse_bulk(text: str):
    found, rejected, seen = [], [], set()
    for lineno, line in enumerate(text.splitlines(), 1):
        candidates = [m.group(0).rstrip(DROP_END) for m in URL_RE.finditer(line)]
        if not candidates and SHORTHAND_RE.fullmatch(line.strip()):
            candidates = [line.strip()]
        for candidate in candidates:
            link = parse_link(candidate)
            if not link:
                rejected.append((lineno, candidate, "Not a supported GitHub or direct HTTPS ZIP link"))
            elif link.key not in seen:
                found.append(link)
                seen.add(link.key)
    return found, rejected


class SafeRedirect(HTTPRedirectHandler):
    """Never forward authorization to a redirected host; only allow HTTPS."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme != "https":
            raise ValueError("Blocked insecure redirect")
        newreq = super().redirect_request(req, fp, code, msg, headers, newurl)
        if newreq is None:
            return None
        if (urlsplit(req.full_url).hostname or "").lower() != parsed.hostname.lower():
            for key in list(newreq.headers):
                if key.lower() in ("authorization", "proxy-authorization"):
                    del newreq.headers[key]
            for key in list(newreq.unredirected_hdrs):
                if key.lower() in ("authorization", "proxy-authorization"):
                    del newreq.unredirected_hdrs[key]
        return newreq


HTTP = build_opener(SafeRedirect())


class GitHubError(Exception):
    pass


class DownloadCancelled(Exception):
    pass


def friendly_http(err: HTTPError):
    code = err.code
    remaining = err.headers.get("X-RateLimit-Remaining", "")
    reset = err.headers.get("X-RateLimit-Reset", "")
    if code in (403, 429) and remaining == "0":
        try:
            seconds = max(0, int(reset) - int(time.time()))
            return f"GitHub API rate limit reached. Try again in ~{seconds // 60 + 1} min or add a token."
        except ValueError:
            return "GitHub API rate limit reached. Add a token or try later."
    if code == 404:
        return "Not found, private without access, or empty repository (HTTP 404)."
    if code in (401, 403):
        return f"Access denied (HTTP {code}); check repository access or GitHub token."
    if code == 422:
        return "GitHub rejected this search or repository reference (HTTP 422)."
    return f"HTTP {code}: {err.reason}"


class GitHubAPI:
    def __init__(self, token=""):
        self.token = token.strip()

    def request(self, path, params=None):
        url = API_BASE + path
        if params:
            url += "?" + urlencode(params)
        req = Request(url, headers=self.headers())
        try:
            with HTTP.open(req, timeout=25) as response:
                return json.load(response)
        except HTTPError as ex:
            raise GitHubError(friendly_http(ex)) from ex
        except (URLError, TimeoutError, OSError, ValueError) as ex:
            raise GitHubError(f"GitHub connection failed: {ex}") from ex

    def headers(self):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        return headers

    def search(self, query, sort="best match", language="", page=1, per_page=30):
        query = query.strip()
        if not query:
            raise GitHubError("Enter a repository search term.")
        if language.strip():
            query += " language:" + language.strip()
        params = {"q": query, "page": page, "per_page": per_page}
        if sort.lower() != "best match":
            params["sort"] = {"most stars": "stars", "recently updated": "updated", "most forks": "forks"}.get(sort.lower(), "stars")
            params["order"] = "desc"
        return self.request("/search/repositories", params)

    def list_profile(self, username, cap=500):
        out = []
        for page in range(1, cap // 100 + 2):
            data = self.request("/users/" + quote(username, safe="") + "/repos", {"type": "owner", "sort": "updated", "per_page": 100, "page": page})
            for repo in data:
                out.append(Link("repo", repo["html_url"], repo["owner"]["login"], repo["name"], label=repo["full_name"]))
                if len(out) >= cap:
                    break
            if len(data) < 100 or len(out) >= cap:
                break
        return out


def archive_request(link: Link, token=""):
    if link.kind == "repo":
        url = f"{API_BASE}/repos/{quote(link.owner, safe='')}/{quote(link.repo, safe='')}/zipball"
        if link.ref:
            url += "/" + quote(link.ref, safe="/")
        return Request(url, headers=GitHubAPI(token).headers())
    return Request(link.url, headers={"User-Agent": USER_AGENT, "Accept": "application/zip"})


def archive_path(link: Link, destination: Path):
    if link.kind == "repo":
        label = filename_safe(link.ref.replace("/", "_") or "default")
        return destination / "Repositories" / filename_safe(link.owner) / filename_safe(link.repo) / f"{filename_safe(link.repo)}__{label}.zip"
    filename = filename_safe(link.filename or "download.zip")
    if not filename.lower().endswith(".zip"):
        filename += ".zip"
    hostname = filename_safe(urlsplit(link.url).hostname or "external")
    if link.owner and link.repo:
        segments = urlsplit(link.url).path.split("/")
        tag = filename_safe(segments[5] if len(segments) > 5 else "releases")
        return destination / "Release_Archives" / filename_safe(link.owner) / filename_safe(link.repo) / tag / filename
    suffix = hashlib.sha256(link.url.encode("utf-8")).hexdigest()[:9]
    return destination / "Direct_ZIPs" / hostname / f"{suffix}__{filename}"


def download_archive(link, destination, token="", on_progress=None, stop=None, pause=None):
    """Streams ZIP to a .part, resumes via Range if supported, then atomically renames."""
    if link.kind == "profile":
        raise GitHubError("GitHub profile URL: expand profile into repositories first.")
    stop = stop or threading.Event()
    pause = pause or threading.Event()
    dest = archive_path(link, Path(destination))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and zipfile.is_zipfile(dest):
        return "Skipped existing", dest, dest.stat().st_size
    part = dest.with_name(dest.name + ".part")
    start = part.stat().st_size if part.exists() else 0
    req = archive_request(link, token)
    if start:
        req.add_header("Range", f"bytes={start}-")
    if stop.is_set():
        raise DownloadCancelled("Stopped")
    try:
        response = HTTP.open(req, timeout=40)
    except HTTPError as ex:
        if ex.code == 416 and start:
            part.unlink(missing_ok=True)
            return download_archive(link, destination, token, on_progress, stop, pause)
        raise GitHubError(friendly_http(ex)) from ex
    except (URLError, OSError, TimeoutError) as ex:
        raise GitHubError(f"Connection failed: {ex}") from ex
    with response:
        status = response.status
        content_range = response.headers.get("Content-Range", "")
        resumable = (status == 206 and content_range.startswith(f"bytes {start}-"))
        if status == 206 and start and not resumable:
            raise GitHubError("Server returned an unexpected resume range; retry after deleting .part file.")
        mode = "ab" if resumable else "wb"
        done = start if resumable else 0
        size_str = response.headers.get("Content-Length", "")
        total = done + int(size_str) if size_str.isdigit() else 0
        before = time.monotonic()
        last_emit = before
        last_done = done
        try:
            with open(part, mode) as out:
                while True:
                    if stop.is_set():
                        raise DownloadCancelled("Stopped; partial file preserved")
                    while pause.is_set():
                        if stop.wait(0.15):
                            raise DownloadCancelled("Stopped; partial file preserved")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if on_progress and now - last_emit >= 0.2:
                        speed = (done - last_done) / max(0.01, now - last_emit)
                        on_progress(done, total, speed)
                        last_done, last_emit = done, now
                out.flush()
                os.fsync(out.fileno())
        except (OSError, TimeoutError) as ex:
            raise GitHubError(f"Transfer interrupted, resumable partial retained: {ex}") from ex
    if on_progress:
        on_progress(done, total, 0)
    if stop.is_set():
        raise DownloadCancelled("Stopped; partial file preserved")
    if not zipfile.is_zipfile(part):
        part.unlink(missing_ok=True)
        raise GitHubError("Downloaded content is not a ZIP archive (server may have returned an HTML page).")
    os.replace(part, dest)
    return "Completed", dest, dest.stat().st_size


def save_manifest(output, rows):
    """Keep human-readable all/failure reports in the chosen output folder."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    import csv
    with (output / "DOWNLOAD_MANIFEST.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Repository or file", "Source URL", "Status", "Saved file", "Bytes", "Error"])
        for row in rows:
            writer.writerow([row.get(k, "") for k in ("name", "url", "status", "path", "size", "error")])
    with (output / "FAILED_LINKS.txt").open("w", encoding="utf-8") as handle:
        for row in rows:
            if row.get("status") == "Failed":
                handle.write(f"{row.get('url', '')}\nReason: {row.get('error', '')}\n\n")
    with (output / "ALL_QUEUED_LINKS.txt").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row.get("url", "") + "\n")
