from rtshf import classify as C
from rtshf.db import DB
from rtshf.util import loads


def test_model_artifact_rules():
    assert C.is_model_artifact("runs/controls/a2c__wf1_2018__s0/model.pt")          # weight formats always count
    assert C.is_model_artifact("data/models/ingame_models.pkl")                     # a pickle under models/
    assert C.is_model_artifact("outputs/models/dcc_macro.npz")
    assert not C.is_model_artifact("data/processed_calibration_panel.pkl")          # data cache
    assert not C.is_model_artifact("results/main/RIDGE.pkl")                        # backtest result object
    assert not C.is_model_artifact("results/q8/weights.pkl")                        # portfolio weights, not a model
    assert not C.is_model_artifact("models/model_inputs.pkl")                       # inputs, even under models/
    assert not C.is_model_artifact("notes/readme.md")


def test_run_folders_collapse_into_one_entry(tmp_path):
    db = DB(tmp_path / "t.db")
    pid = db.insert("projects", {"slug": "lab", "name": "lab"})
    sid = db.insert("sources", {"kind": "local", "key": "local:lab", "name": "lab", "project_id": pid})
    files = [f"runs/main/{a}__wf{w}__s{s}/model.pt" for a in ("dqn", "ppo") for w in (1, 2, 3) for s in (0, 1)]
    files += ["models/clf.joblib", "results/main/GBM.pkl", "data/cache.pkl"]
    for f in files:
        db.insert("file_index", {"source_id": sid, "path": f, "size": 10, "mtime": 1.7e9, "role": "artifact", "status": "active"})
    assert C.refresh_model_registry(db, pid, "lab") == 2
    rows = {r["name"]: r for r in db.all("SELECT * FROM models")}
    runs = rows["runs/main"]
    meta = loads(runs["meta"], {})
    assert runs["artifacts"] == 12 and meta["runs"] == 12
    assert [f["n"] for f in meta["facets"]] == [2, 3, 2]                          # 2 agents x 3 windows x 2 seeds
    assert rows["models"]["kind"] == "joblib model"
    first = runs["first_seen"]
    C.refresh_model_registry(db, pid, "lab")                                     # re-running keeps first_seen
    assert db.one("SELECT first_seen FROM models WHERE name='runs/main'")["first_seen"] == first
