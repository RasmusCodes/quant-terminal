from pathlib import Path

from rtshf.connectors import files as FW
from rtshf.export import scan


def _touch(p: Path, text: str = "x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_walk_rules(tmp_path):
    root = tmp_path / "proj"
    _touch(root / "README.md", "# Proj")
    _touch(root / "reports" / "REPORT.md", "# Report")
    _touch(root / "reports" / "dashboard.html", "<html></html>")
    _touch(root / ".env", "SECRET=1")
    _touch(root / "secrets" / "key.md", "nope")
    _touch(root / ".venv" / "lib" / "x.md", "nope")
    _touch(root / "data" / "raw" / "dump.md", "nope")
    _touch(root / "data" / "book.csv", "ticker,shares,ref_price\nA,1,2\n")
    _touch(root / "research" / "transcript_q2.txt", "third party")
    _touch(root / "research" / "jereh_h1.pdf", "%PDF")
    _touch(root / "out" / "memo.pdf", "%PDF")
    _touch(root / "engine" / "vendor_lib" / "LICENSE", "MIT")
    _touch(root / "engine" / "vendor_lib" / "README.md", "vendored")
    _touch(root / "src" / "model.py", "import pandas")
    _touch(root / "private" / "ledger.md", "amounts")
    _touch(root / "skipme" / ".rtsignore", "")
    _touch(root / "skipme" / "notes.md", "nope")
    entries, stats = FW.walk(root)
    rels = {e.rel for e in entries}
    assert "README.md" in rels and "reports/REPORT.md" in rels and "out/memo.pdf" in rels and "src/model.py" in rels
    assert "data/book.csv" in rels
    for bad in (".env", "secrets/key.md", ".venv/lib/x.md", "data/raw/dump.md", "research/transcript_q2.txt",
                "research/jereh_h1.pdf", "reports/dashboard.html", "engine/vendor_lib/README.md", "private/ledger.md", "skipme/notes.md"):
        assert bad not in rels, bad


def test_github_slug():
    assert FW.github_slug("https://github.com/RasmusCodes/hft-sim.git") == "RasmusCodes/hft-sim"
    assert FW.github_slug("git@github.com:RasmusCodes/figgie-bot.git") == "RasmusCodes/figgie-bot"


def test_privacy_scan(tmp_path):
    out = tmp_path / "site"
    _touch(out / "data" / "a.json", '{"path": "C:\\\\Users\\\\someone\\\\Documents\\\\proj\\\\x"}')
    _touch(out / "data" / "b.json", '{"note": "contact me at someone@acme-mail.co"}')
    _touch(out / "data" / "e.json", '{"note": "placeholder you@example.com.evil.co"}')
    _touch(out / "data" / "c.json", '{"repo": "secret-project"}')
    _touch(out / "data" / "d.json", '{"url": "https://github.com/RasmusCodes/garden"}')
    _touch(out / "data" / "ok.json", '{"repo": "hft-sim", "text": "garden notes and a kitchen page", '
                                     '"author": "95073323+RasmusCodes@users.noreply.github.com", '
                                     '"ua": "SEC_UA=\'Your Name you@example.com\'", "dev": "admin@staging.test"}')
    hits = scan(out, {"secret-project", "garden", "kitchen"})
    kinds = {(h["file"].replace("\\", "/"), h["pattern"]) for h in hits}
    assert ("data/a.json", "windows_path") in kinds
    assert ("data/b.json", "email") in kinds
    assert ("data/e.json", "email") in kinds                       # a reserved name inside a real domain is not reserved
    assert ("data/c.json", "private_repo_name") in kinds
    assert ("data/d.json", "private_repo_name") in kinds          # a link to a private repo is caught even for common words
    assert not any(h["file"].endswith("ok.json") for h in hits)     # the common words themselves are not
