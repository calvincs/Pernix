"""The deploy-detection stamp must survive images without .git — the content
hash stands in for the sha, so a same-version rebuild still reads as a deploy."""

from __future__ import annotations


def test_code_hash_shape_and_stability():
    from api.app import _compute_code_hash

    h1 = _compute_code_hash()
    h2 = _compute_code_hash()
    assert h1 == h2, "must be deterministic for a given code state"
    assert len(h1) == 12
    int(h1, 16)  # hex


def test_code_hash_changes_with_code(tmp_path, monkeypatch):
    # Point the hasher at a fake repo root and prove a code edit moves it.
    import api.app as app_mod

    root = tmp_path
    (root / "api").mkdir()
    (root / "core").mkdir()
    (root / "sessions").mkdir()
    (root / "db").mkdir()
    (root / "static").mkdir()
    (root / "config.py").write_text("x = 1\n")
    (root / "run.py").write_text("pass\n")
    (root / "maintenance.py").write_text("pass\n")
    (root / "core" / "a.py").write_text("def f(): return 1\n")

    real_file = app_mod.__file__

    # _compute_code_hash derives the repo root from the module's __file__ at
    # call time — point it into the fake root.
    (root / "api" / "app.py").write_text("# fake\n")
    monkeypatch.setattr(app_mod, "__file__", str(root / "api" / "app.py"))
    try:
        h1 = app_mod._compute_code_hash()
        (root / "core" / "a.py").write_text("def f(): return 2\n")
        h2 = app_mod._compute_code_hash()
    finally:
        monkeypatch.setattr(app_mod, "__file__", real_file)
    assert h1 != h2
