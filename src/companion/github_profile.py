"""Pulls Duc's real public GitHub projects (repo name, description,
language, README excerpt) to use as supplementary resume/draft
material - on top of an uploaded resume, not instead of one, and never
guessed at. GitHub's REST API is public and needs no auth for public
data, same "official API, not scraping" reasoning as feeds.py (see
CLAUDE.md's tech-news decision) - LinkedIn doesn't offer an equivalent
for a personal script without an approved OAuth app, which is why
there's no linkedin_profile.py alongside this: see docs/agentic-
roadmap.md for that call.
"""
import json
import re
import urllib.error
import urllib.request

API_BASE = "https://api.github.com"
USER_AGENT = "Kyra-companion/1.0 (personal project, github.com/anthropics not affiliated)"
README_EXCERPT_CHARS = 500


def extract_username(github_url: str) -> str | None:
    """"https://github.com/minhducvo04" -> "minhducvo04". None if it
    doesn't look like a GitHub profile URL at all.
    """
    match = re.search(r"github\.com/([A-Za-z0-9-]+)/?$", github_url.strip().rstrip("/"))
    return match.group(1) if match else None


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_readme_excerpt(owner: str, repo: str) -> str:
    import base64

    try:
        data = _get_json(f"{API_BASE}/repos/{owner}/{repo}/readme")
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        # Strip markdown image/badge lines and headers - noise for this
        # purpose, just want the actual descriptive prose.
        lines = [ln for ln in content.splitlines() if not ln.strip().startswith(("![", "[![", "#"))]
        text = " ".join(ln.strip() for ln in lines if ln.strip())
        return text[:README_EXCERPT_CHARS]
    except (urllib.error.HTTPError, KeyError, ValueError):
        return ""  # no README, private/malformed content, etc. - not fatal


def fetch_github_projects(username: str, max_repos: int = 6) -> tuple[str, list[str]]:
    """Returns (formatted_text, warnings). Only real repo data - no
    invented descriptions. Forks are excluded (not Duc's original
    work); sorted by most-recently-pushed so the result favors active,
    current projects over old ones. A README fetch failure for one repo
    is a warning, not a hard failure - the rest still comes through.
    """
    warnings: list[str] = []
    try:
        repos = _get_json(f"{API_BASE}/users/{username}/repos?per_page=100&type=owner&sort=pushed")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "", [f"no GitHub user found for '{username}'"]
        return "", [f"GitHub API error fetching repos: {e.code}"]
    except Exception as e:
        return "", [f"couldn't reach GitHub: {e}"]

    real_repos = [r for r in repos if not r.get("fork")][:max_repos]
    if not real_repos:
        return "", [f"'{username}' has no public non-fork repositories"]

    parts = []
    for repo in real_repos:
        name = repo["name"]
        description = repo.get("description") or ""
        language = repo.get("language") or ""
        stars = repo.get("stargazers_count", 0)
        readme = _get_readme_excerpt(username, name)

        block = f"[{name}]"
        if language:
            block += f" ({language})"
        if stars:
            block += f" - {stars} stars"
        if description:
            block += f"\n{description}"
        if readme:
            block += f"\nFrom README: {readme}"
        parts.append(block)

    return "\n\n".join(parts), warnings
