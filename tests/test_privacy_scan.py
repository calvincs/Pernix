"""scripts/privacy_scan.py — the gate that keeps personal data and secrets out of git.

Trigger strings are assembled at runtime so this file passes the scanner itself.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "privacy_scan", Path(__file__).resolve().parent.parent / "scripts" / "privacy_scan.py"
)
ps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ps)


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    terms = tmp_path / "terms.txt"
    terms.write_text("# comment\nZanzibarville\nre:\\bACME-\\d{4}\\b\n")
    monkeypatch.setattr(ps, "TERMS_FILE", terms)
    monkeypatch.setattr(ps, "ALLOW_FILE", tmp_path / "none")
    return ps.Scanner()


def _kinds(s):
    return [f.split(": ")[1] for f in s.findings]


def test_private_terms_block_and_are_not_echoed(scanner):
    scanner.text("x", "a.py", "the user lives in zanzibarville, ticket ACME-1234")
    assert _kinds(scanner) == ["private term", "private term"]
    assert all("zanzibarville" not in f.lower() for f in scanner.findings)


@pytest.mark.parametrize(
    "text",
    [
        "mail me at someone" + "@" + "realdomain.io",
        "host " + "192.168" + ".7.23 on the lan",
        "cat /home/" + "jsmith" + "/notes.txt",
        "call 555" + "-867-5309",
        "at 41.2" + "5432, -89.1" + "2345",
        "-----BEGIN " + "RSA PRIVATE KEY-----",
        "key = AKIA" + "ABCDEFGHIJKLMNOP",
        "ghp_" + "a1" * 18,
        'api_key = "' + "Ab3" * 8 + '"',
    ],
)
def test_personal_data_and_secrets_are_caught(scanner, text):
    scanner.text("x", "a.py", text)
    assert scanner.findings, text


@pytest.mark.parametrize(
    "text",
    [
        "see user" + "@example.com and 1234+x" + "@users.noreply.github.com",
        "cd /home/user/project and /Users/<user>/x",
        'token:"delimiter.bracket"',
        "version 3.4.2, port 8090, 2026-11-06",
    ],
)
def test_generic_values_pass(scanner, text):
    scanner.text("x", "a.py", text)
    assert scanner.findings == []


def test_runtime_data_files_are_refused(scanner):
    for p in ("data/memories/user.profile.md", "data/settings.json", "x/state.db", ".env"):
        scanner.path("x", p)
    assert len(scanner.findings) == 4
    scanner.findings.clear()
    scanner.path("x", ".env.example")
    assert scanner.findings == []


def test_coauthor_rule_requires_all_three(scanner):
    scanner.coauthors("m", "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
    assert "missing: codex, pernix" in scanner.findings[0]
    scanner.findings.clear()
    scanner.coauthors(
        "m",
        "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>\nCo-Authored-By: Codex <noreply@openai.com>\n"
        "Co-Authored-By: Pernix <noreply@pernix.cc>\n",
    )
    scanner.coauthors("m", "fix with no credits")
    assert scanner.findings == []


def test_allow_file_entries(tmp_path, monkeypatch):
    allow = tmp_path / "allow"
    allow.write_text("path:LICENSE\nre:^10\\.0\\.0\\.5$\n")
    monkeypatch.setattr(ps, "ALLOW_FILE", allow)
    monkeypatch.setattr(ps, "TERMS_FILE", tmp_path / "none")
    s = ps.Scanner()
    s.text("x", "a.py", "server 10.0" + ".0.5")
    s.text("x", "LICENSE", "mail " + "x" + "@realdomain.io")
    assert s.findings == []


def test_image_metadata_is_detected(tmp_path):
    jpg = tmp_path / "a.jpeg"
    jpg.write_bytes(b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00rest")
    clean = tmp_path / "b.png"
    clean.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x00IEND")
    assert ps._image_metadata(jpg) and not ps._image_metadata(clean)
