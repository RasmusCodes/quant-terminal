from rtshf.db import DB
from rtshf.ingest import refresh_superseded
from rtshf.util import in_superseded_folder


def test_superseded_folder_rule():
    for old in ("archive/memo.md", "runs/_archive/a/model.pt", "data/models_v1/report.json", "x/old/y.md",
                "results/pairs_study_v1_pre_rename_fix/tables/t.csv", "out/pre-bugfix/r.json", "runs/before_fix/r.json"):
        assert in_superseded_folder(old), old
    for cur in ("results/pairs_study/tables/t.csv", "runs/hpo_ppo_spy_v2/a/model.pt", "src/prefix/notes.md",
                "notes/oldham.md", "v1/readme.md", "archive.md", "reports/old_vs_new.md"):
        assert not in_superseded_folder(cur), cur


def test_old_results_stop_counting_but_stay_in_history(tmp_path):
    db = DB(tmp_path / "t.db")
    sid = db.insert("sources", {"kind": "local", "key": "local:lab", "name": "lab"})
    old = db.insert("documents", {"source_id": sid, "path": "results/study_v1_pre_rename_fix/headline.json", "status": "active", "superseded": 0})
    new = db.insert("documents", {"source_id": sid, "path": "results/study/headline.json", "status": "active", "superseded": 0})
    db.insert("experiments", {"fingerprint": "a", "document_id": old, "name": "pairs Sharpe", "status": "active"})
    db.insert("experiments", {"fingerprint": "b", "document_id": new, "name": "pairs Sharpe", "status": "active"})
    out = refresh_superseded(db)
    assert out == {"documents_flipped": 1, "experiments_superseded": 1, "experiments_restored": 0}
    status = {r["fingerprint"]: r["status"] for r in db.all("SELECT fingerprint, status FROM experiments")}
    assert status == {"a": "superseded", "b": "active"}
    assert refresh_superseded(db) == {"documents_flipped": 0, "experiments_superseded": 0, "experiments_restored": 0}
