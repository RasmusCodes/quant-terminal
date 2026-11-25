from rtshf import assistant as A
from rtshf import health as H
from rtshf.db import DB


class _Settings:
    is_public = False
    owner = "someone"


def _failing(n):
    return {"failing": True, "failed_runs": n, "since": "2026-11-23T07:50:05Z", "first_failed_id": 7,
            "failed_step": "Export (privacy-scanned)", "last_success": "2026-11-23T01:22:44Z",
            "latest": {"url": "https://github.com/someone/quant-terminal/actions/runs/9"}}


def test_public_build_alert_opens_once_and_resolves_itself(tmp_path, monkeypatch):
    db = DB(tmp_path / "t.db")
    rid = db.insert("sync_runs", {"started_at": "2026-11-25T00:00:00+00:00", "mode": "private", "trigger": "test", "status": "running"})
    monkeypatch.setattr(H, "public_build", lambda owner, **k: _failing(1))
    assert A._public_build(db, rid, _Settings()) == 0                      # one failure can be a hiccup: no alert
    monkeypatch.setattr(H, "public_build", lambda owner, **k: _failing(3))
    assert A._public_build(db, rid, _Settings()) == 1
    assert A._public_build(db, rid, _Settings()) == 0                      # same streak: updated, not duplicated
    a = db.one("SELECT * FROM alerts WHERE dedupe_key='sys:public-build:7'")
    assert a["status"] == "new" and a["basis"] == "system" and "privacy scan" in a["detail"]
    monkeypatch.setattr(H, "public_build", lambda owner, **k: {"failing": False, "failed_runs": 0, "latest": {}, "last_success": "x"})
    A._public_build(db, rid, _Settings())
    assert db.one("SELECT status FROM alerts WHERE dedupe_key='sys:public-build:7'")["status"] == "resolved"
