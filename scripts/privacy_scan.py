#!/usr/bin/env python3
"""Pernix — block personal data and secrets before they reach git history.

Run by the hooks in .githooks/ (enable once with `git config core.hooksPath .githooks`):

  pre-commit  -> privacy_scan.py staged        added lines + paths of the staged change
  commit-msg  -> privacy_scan.py message FILE  the commit message (+ the co-author rule)
  pre-push    -> privacy_scan.py push          every commit being pushed: diff, message
  manual      -> privacy_scan.py tree          audit every tracked file

What it looks for:
  * private terms — one per line in ~/.config/pernix/private-terms.txt (outside the
    repo, so the list itself is never committed). Plain text is matched
    case-insensitively; a line starting with `re:` is a regular expression.
  * personal-data patterns — email addresses, private IPv4 addresses, home-directory
    paths, phone numbers, GPS coordinates.
  * secrets — private keys, cloud/API tokens, hard-coded passwords.
  * files that must never be committed — runtime data (data/memories, *.db,
    settings.json), .env, key files.
  * the co-author rule — when a message credits a collaborator, it credits all
    three: Claude, Codex and Pernix.

Known-benign matches go in .privacy-allow (in the repo; generic values only):
  re:<regex>      a matching finding is ignored
  path:<glob>     findings in matching files are ignored

Exit 1 with a report on any finding. `--no-verify` skips the hooks; it is only
used with the repository owner's explicit approval.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(
    subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip() or "."
)
TERMS_FILE = Path(os.environ.get("PERNIX_PRIVATE_TERMS", Path.home() / ".config/pernix/private-terms.txt"))
ALLOW_FILE = ROOT / ".privacy-allow"

# ── patterns ────────────────────────────────────────────────────────────

_SAFE_EMAIL_DOMAINS = (
    "example.com", "example.org", "example.net", "example.internal", "invalid", "localhost", "test",
    "users.noreply.github.com", "noreply.github.com", "anthropic.com", "openai.com", "pernix.cc",
)  # fmt: skip
_SAFE_HOME_USERS = {"user", "users", "ubuntu", "runner", "me", "you", "example", "app", "root", "pernix", "<user>",
                    "username", "name", "jdoe", "alice", "bob"}  # fmt: skip

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email address", re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")),
    ("private IP address", re.compile(r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b")),
    ("home-directory path", re.compile(r"(?:/home|/Users)/([A-Za-z0-9._<>-]+)/")),
    ("phone number", re.compile(r"(?<![\d.])(?:\+1[\s.-]?)?\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b(?![\d.])")),
    ("GPS coordinates", re.compile(r"(?<![\d.])-?\d{1,2}\.\d{4,},\s*-?\d{1,3}\.\d{4,}(?![\d.])")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("API key (sk-)", re.compile(r"\bsk-(?:ant-|proj-|or-)?[A-Za-z0-9_-]{24,}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("bearer token", re.compile(r"(?i)\bauthorization:\s*bearer\s+[A-Za-z0-9._~+/=-]{24,}")),
    (
        "hard-coded secret",
        re.compile(r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['\"][A-Za-z0-9/+=_.-]{16,}['\"]"),
    ),
]

FORBIDDEN_PATHS = [
    "data/memories/*", "data/sessions*", "data/workspace/*", "data/logs/*", "data/backups/*", "data/.backups/*",
    "data/settings.json", "*.db", "*.sqlite", "*.sqlite3", ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
    "*.p12", "*.pfx",
]  # fmt: skip
_FORBIDDEN_OK = {".env.example"}
_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"}
_METADATA_EXT = {".pdf", ".docx", ".xlsx", ".pptx", ".mov", ".mp4"}


def _image_metadata(p: Path) -> bool:
    """True when an image file carries EXIF, XMP or PNG text chunks."""
    try:
        b = p.read_bytes()
    except OSError:
        return False
    if b"Exif\x00\x00" in b or b"<x:xmpmeta" in b or b"http://ns.adobe.com/xap/" in b:
        return True
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return any(t in b for t in (b"eXIf", b"tEXt", b"iTXt", b"zTXt"))
    return False


# ── config files ────────────────────────────────────────────────────────


def load_terms() -> list[tuple[str, re.Pattern]]:
    out = []
    if not TERMS_FILE.exists():
        return out
    for line in TERMS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            out.append(("private term", re.compile(line[3:], re.IGNORECASE)))
        else:
            out.append(("private term", re.compile(re.escape(line), re.IGNORECASE)))
    return out


def load_allow() -> tuple[list[re.Pattern], list[str]]:
    regs, paths = [], []
    if ALLOW_FILE.exists():
        for line in ALLOW_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("re:"):
                regs.append(re.compile(line[3:]))
            elif line.startswith("path:"):
                paths.append(line[5:].strip())
    return regs, paths


# ── scanning ────────────────────────────────────────────────────────────


class Scanner:
    def __init__(self):
        self.terms = load_terms()
        self.allow_re, self.allow_paths = load_allow()
        self.findings: list[str] = []
        self.warnings: list[str] = []

    def _allowed(self, path: str, text: str) -> bool:
        if path and any(fnmatch.fnmatch(path, g) for g in self.allow_paths):
            return True
        return any(r.search(text) for r in self.allow_re)

    def _benign(self, kind: str, m: re.Match) -> bool:
        s = m.group(0)
        if kind == "email address":
            dom = s.split("@", 1)[1].lower()
            return any(dom == d or dom.endswith("." + d) for d in _SAFE_EMAIL_DOMAINS)
        if kind == "home-directory path":
            return m.group(1).lower() in _SAFE_HOME_USERS
        if kind == "hard-coded secret":
            # A real secret mixes letters and digits; identifiers and
            # dotted names ("delimiter.bracket") do not.
            val = re.search(r"['\"]([^'\"]+)['\"]", s)
            v = val.group(1) if val else ""
            return "." in v or not (re.search(r"\d", v) and re.search(r"[A-Za-z]", v))
        return False

    def text(self, where: str, path: str, text: str) -> None:
        for kind, rx in [*self.terms, *PATTERNS]:
            for m in rx.finditer(text):
                if self._benign(kind, m):
                    continue
                snippet = text[max(0, m.start() - 30) : m.end() + 30].replace("\n", " ")
                if self._allowed(path, m.group(0)) or self._allowed(path, snippet):
                    continue
                # Never echo a private term back in full: the report can end up in logs.
                shown = m.group(0) if kind != "private term" else m.group(0)[:2] + "…"
                self.findings.append(f"{where}: {kind}: {shown!r}")

    def path(self, where: str, path: str) -> None:
        name = os.path.basename(path)
        if name not in _FORBIDDEN_OK and any(
            fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in FORBIDDEN_PATHS
        ):
            if not self._allowed(path, path):
                self.findings.append(f"{where}: file must not be committed: {path}")
        ext = Path(path).suffix.lower()
        if ext in _IMAGE_EXT:
            if _image_metadata(ROOT / path):
                self.findings.append(
                    f"{where}: {path} carries EXIF/XMP/text metadata (software, time zone, maybe location) — strip it"
                )
        elif ext in _METADATA_EXT:
            self.warnings.append(
                f"{where}: {path} may carry document metadata (author, location) — check before committing"
            )
        self.text(where, path, path)

    def diff(self, where: str, diff: str) -> None:
        path = ""
        for line in diff.splitlines():
            if line.startswith("+++ "):
                path = line[6:] if line.startswith("+++ b/") else ""
                if path:
                    self.path(where, path)
            elif line.startswith("+") and not line.startswith("+++"):
                self.text(f"{where} {path}", path, line[1:])

    def coauthors(self, where: str, message: str) -> None:
        lines = [ln for ln in message.splitlines() if ln.lower().startswith("co-authored-by:")]
        if not lines:
            return
        joined = "\n".join(lines).lower()
        missing = [n for n in ("claude", "codex", "pernix") if n not in joined]
        if missing:
            self.findings.append(
                f"{where}: co-author rule: credit all of Claude, Codex and Pernix, or none (missing: {', '.join(missing)})"
            )


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT).stdout


def scan_staged(s: Scanner) -> None:
    s.diff("staged", git("diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR"))


def scan_message(s: Scanner, msg_file: str) -> None:
    raw = Path(msg_file).read_text(encoding="utf-8", errors="replace")
    msg = "\n".join(ln for ln in raw.splitlines() if not ln.startswith("#"))
    s.text("commit message", "", msg)
    s.coauthors("commit message", msg)


def scan_push(s: Scanner, stdin: str) -> None:
    zero = "0" * 40
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if local_sha == zero:
            continue  # deleting a ref
        rng = [local_sha, "--not", "--remotes"] if remote_sha == zero else [f"{remote_sha}..{local_sha}"]
        for sha in git("rev-list", *rng).split():
            short = sha[:9]
            s.diff(f"commit {short}", git("show", "-U0", "--no-color", "--format=", sha))
            msg = git("log", "-1", "--format=%B", sha)
            s.text(f"commit {short} message", "", msg)
            s.coauthors(f"commit {short} message", msg)
            email = git("log", "-1", "--format=%ae", sha).strip()
            if email and not email.endswith("users.noreply.github.com"):
                s.warnings.append(
                    f"commit {short}: author email is not a GitHub noreply address (it is public once pushed)"
                )


def scan_tree(s: Scanner) -> None:
    for path in git("ls-files").splitlines():
        p = ROOT / path
        if not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        s.path("tree", path)
        try:
            s.text(f"tree {path}", path, p.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            continue


def gitleaks(mode: str) -> list[str]:
    """Defer to gitleaks for credentials when it is installed (optional)."""
    exe = shutil.which("gitleaks")
    if not exe or mode not in ("staged", "push"):
        return []
    args = (
        [exe, "git", "--staged", "--no-banner", "--redact"]
        if mode == "staged"
        else [exe, "git", "--no-banner", "--redact", "--log-opts=-1"]
    )
    r = subprocess.run(args, capture_output=True, text=True, cwd=ROOT)
    return [f"gitleaks: {r.stdout.strip() or r.stderr.strip()}"] if r.returncode == 1 else []


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    mode = argv[0]
    s = Scanner()
    if not s.terms:
        s.warnings.append(f"no private terms loaded — create {TERMS_FILE} (one term per line)")
    if mode == "staged":
        scan_staged(s)
    elif mode == "message":
        scan_message(s, argv[1])
    elif mode == "push":
        scan_push(s, sys.stdin.read())
    elif mode == "tree":
        scan_tree(s)
    else:
        print(f"unknown mode {mode!r}")
        return 2
    s.findings += gitleaks(mode)
    for w in dict.fromkeys(s.warnings):
        print(f"privacy-scan warning: {w}", file=sys.stderr)
    if s.findings:
        print("privacy-scan BLOCKED — personal data or secrets found:", file=sys.stderr)
        for f in dict.fromkeys(s.findings):
            print(f"  - {f}", file=sys.stderr)
        print(
            "Replace them with example values (example.com, /home/user/, 192.0.2.x), or add a generic allow rule to "
            ".privacy-allow. Do not use --no-verify without the owner's approval.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
