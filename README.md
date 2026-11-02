# GitHub Archive Studio v1.0

**Standalone GUI for finding repositories and downloading ZIP source archives.** Works on Windows with Python 3.10+ and Tkinter; no pip packages required. Also runs on Linux/macOS where Tkinter is installed.

## Windows quick start

1. Extract the ZIP.
2. Double-click `RUN_WINDOWS.bat`.
3. In **Search**, enter a phrase and click **Search Repositories**, select rows, and click **Queue Selected ZIPs**. You can also queue all results shown on a page.
4. In **Mass TXT Links**, click **Load TXT File** or **Load bundled GitHub list** (the extracted links you provided previously). Click **Import links to queue** or **Import and start downloads**.
5. Go to **Download Queue**, choose your destination, and click **START QUEUE**. Use Open Downloads to view saved ZIPs. Resume failed/stopped jobs using Retry failed and Start.

## Download structure

```text
<selected folder>/
  Repositories/<owner>/<repository>/<repository>__default.zip
  Repositories/<owner>/<repository>/<repository>__<branch>.zip
  Release_Archives/<owner>/<repository>/<release-tag>/<filename>.zip
  Direct_ZIPs/<host>/<url-hash>__<filename>.zip
  DOWNLOAD_MANIFEST.csv
  ALL_QUEUED_LINKS.txt
  FAILED_LINKS.txt
  UNSUPPORTED_LINKS.txt (created when needed)
```

ZIP archives are *not extracted or executed*. Repositories use GitHub's official REST `zipball` endpoint, so this is a source snapshot rather than a Git clone including commit history and `.git` metadata.

## Supported TXT input

- One link per line, or URLs scattered throughout normal text (including Telegram exports).
- `https://github.com/OWNER/REPO` and `OWNER/REPO` shorthand.
- GitHub archive and API zipball links. Branch URLs are recognized where available.
- `https://github.com/OWNER` GitHub profile URLs: lists up to 500 public repositories per profile and adds them to the queue, subject to API rate limits.
- GitHub release asset `.zip` URLs and **direct HTTPS URLs ending in .zip**.
- Repeated references to the same repo and branch are deduplicated, and valid downloaded ZIPs are skipped on subsequent runs even if the app is restarted.

Generic webpages, GitHub issue pages as individual assets, cloud-storage folders, Git LFS object expansions, submodules, and non-ZIP direct media are **not** crawled. A repository link downloads its GitHub-generated source archive, not its releases, clone history, or external dependencies. A repository without public read permission may fail.

## Optional GitHub token

Search and public ZIP downloads work without a token, but GitHub API rate limits apply. Create a GitHub personal access token in GitHub Settings if needed. Enter it in the **GitHub Access** tab or set the environment variable `GITHUB_TOKEN`. Token is used for GitHub API calls only and is never stored to disk or included in reports. Use the least permissions needed; if private repos are needed, grant **Contents: read** only for repositories you own or are authorized to access.

**Important:** Do not paste a token into a mass TXT list. Do not share `GITHUB_TOKEN` or login credentials. GitHub API errors will show in the GUI when rate-limited or unauthorized.

## Reliability and limits

- `*.zip.part` files are kept when an active transfer is stopped or disconnected; downloads attempt Range resume and restart cleanly if the server does not support it.
- Existing valid ZIP archives are skipped. An incomplete/non-ZIP response is **not** marked as completed.
- The queue has Pause/Resume, Stop, Retry failed, duplicate detection, 1–6 parallel workers, live bytes/speed, output folder chooser, and CSV/TXT reports.
- Re-running an identical TXT list is safe. The app does not save its live in-memory queue across restarts; reimport the TXT list to check for and skip archives already present.
- If the repository's default branch changes after the first archive, the `__default.zip` name will not automatically detect that change. Delete that archive to get a fresh snapshot.
- Network access is required. Internet download success and any external GitHub permission are not guaranteed by offline tests.

Official references: https://docs.github.com/en/rest/search/search and https://docs.github.com/en/rest/repos/contents
