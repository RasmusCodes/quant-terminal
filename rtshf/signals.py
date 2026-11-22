"""Signal registry: one standard record for quant outputs, alt-data screens and fundamental theses.

Standard fields: strategy_id, instrument, ts, available_at (when the information was knowable), horizon,
direction (-1/0/1), expected_return, confidence, risk, model_version, provenance, validation_ref/status.
Projects classified as game theory never produce investment signals.
"""
from __future__ import annotations

from collections import defaultdict

from .db import DB
from .extract import signals_sniffer as S
from .util import dumps, loads, now_iso, sha1

VALIDATION_WEIGHT = {"validated_oos": 1.0, "backtested": 0.7, "fundamental": 0.8, "unvalidated": 0.4}


def ingest_table(ctx, skey: str, project_id, mapping: dict, raw: str, provenance: dict) -> int:
    db: DB = ctx.db
    cat = db.scalar("SELECT relevance FROM projects WHERE id=?", (project_id,)) if project_id else None
    if cat == "game_theory":
        return 0
    rows = S.load_signals(raw, mapping)
    db.execute("UPDATE signals SET active=0 WHERE strategy_id=?", (skey,))
    n = 0
    for r in rows:
        if not r["direction"] and r["value"] is None:
            continue
        ts = r.get("as_of") or now_iso()[:10]
        fp = sha1(skey, r["instrument"], ts, r["value"], r["direction"])
        db.upsert("signals", {"fingerprint": fp, "strategy_id": skey, "instrument": r["instrument"], "ts": ts,
                              "available_at": provenance.get("modified") or now_iso(), "horizon": mapping.get("horizon") or "next period",
                              "direction": r["direction"], "expected_return": r["expected_return"], "confidence": r["confidence"],
                              "risk": None, "value": r["value"], "model_version": (provenance.get("hash") or "")[:12],
                              "provenance": dumps(provenance), "validation_ref": None, "validation_status": "unvalidated",
                              "project_id": project_id, "source_kind": "project_output",
                              "meta": dumps({"z": r["z"], "column": mapping.get("value_col")}), "created_at": now_iso(), "active": 1},
                  ["fingerprint"])
        n += 1
    ctx.stats["signals"] += n
    return n


def thesis_signals(db: DB) -> int:
    """One fundamental signal per active thesis (direction from the stance, expected return from the target)."""
    db.execute("UPDATE signals SET active=0 WHERE source_kind='thesis'")
    n = 0
    for t in db.all("SELECT t.*, p.slug, d.url, d.path, d.modified FROM theses t LEFT JOIN projects p ON p.id=t.project_id "
                    "LEFT JOIN documents d ON d.id=t.document_id WHERE t.status='active'"):
        direction = {"long": 1, "short": -1}.get(t["stance"], 0)
        scen = loads(t["scenarios"], {}) or {}
        risk = None
        if t["price_at_thesis"] and scen.get("bear"):
            risk = round(scen["bear"] / t["price_at_thesis"] - 1, 4) if direction >= 0 else \
                round(1 - (scen.get("bull") or t["price_at_thesis"]) / t["price_at_thesis"], 4)
        sid = f"thesis:{t['slug'] or t['project_id']}"
        fp = sha1(sid, t["symbol"], t["thesis_date"], t["target_price"], t["stance"])
        db.upsert("signals", {"fingerprint": fp, "strategy_id": sid, "instrument": t["symbol"], "ts": t["thesis_date"],
                              "available_at": t["thesis_date"] or t["modified"], "horizon": t["horizon"] or "12m",
                              "direction": direction, "expected_return": t["upside"],
                              "confidence": 0.6 if t["target_price"] else 0.45, "risk": risk, "value": t["target_price"],
                              "model_version": f"thesis v{t['version'] or 1}",
                              "provenance": dumps({"document": t["path"], "url": t["url"], "thesis_id": t["id"]}),
                              "validation_ref": f"thesis:{t['id']}", "validation_status": "fundamental",
                              "project_id": t["project_id"], "source_kind": "thesis",
                              "meta": dumps({"rating": t["rating"], "scenarios": scen}), "created_at": now_iso(), "active": 1},
                  ["fingerprint"])
        n += 1
    db.commit()
    return n


def attach_validation(db: DB) -> None:
    """Link each project-output signal to the strongest experiment of its project (if any)."""
    for p in db.all("SELECT DISTINCT project_id FROM signals WHERE source_kind='project_output' AND project_id IS NOT NULL"):
        best = db.one("""SELECT e.id, e.oos, e.name,
                                MAX(CASE WHEN m.name='t_stat' THEN ABS(m.value) END) AS t,
                                MAX(CASE WHEN m.name='p_value' THEN m.value END) AS p
                         FROM experiments e LEFT JOIN experiment_metrics m ON m.experiment_id=e.id
                         WHERE e.project_id=? AND e.status='active' GROUP BY e.id
                         ORDER BY e.oos DESC, t DESC LIMIT 1""", (p["project_id"],))
        if not best:
            continue
        status = "validated_oos" if best["oos"] and ((best["t"] or 0) >= 2 or (best["p"] is not None and best["p"] < 0.05)) else "backtested"
        db.execute("UPDATE signals SET validation_ref=?, validation_status=? WHERE project_id=? AND source_kind='project_output'",
                   (f"experiment:{best['id']}", status, p["project_id"]))
    db.commit()


def aggregate(db: DB) -> list[dict]:
    """Per instrument: confidence- and validation-weighted direction, with every contributing signal listed."""
    rows = db.all("""SELECT s.*, p.slug, p.name AS project_name, p.relevance FROM signals s
                     LEFT JOIN projects p ON p.id=s.project_id WHERE s.active=1""")
    by = defaultdict(list)
    for r in rows:
        if r["relevance"] == "game_theory":
            continue
        by[r["instrument"]].append(r)
    out = []
    for sym, sigs in by.items():
        num = den = 0.0
        contrib = []
        for s in sigs:
            w = (s["confidence"] or 0.3) * VALIDATION_WEIGHT.get(s["validation_status"] or "unvalidated", 0.4)
            num += w * (s["direction"] or 0)
            den += w
            contrib.append({"strategy": s["strategy_id"], "project": s["slug"], "direction": s["direction"],
                            "confidence": s["confidence"], "validation": s["validation_status"], "expected_return": s["expected_return"],
                            "value": s["value"], "horizon": s["horizon"], "ts": s["ts"], "kind": s["source_kind"]})
        score = num / den if den else 0.0
        dirs = {c["direction"] for c in contrib if c["direction"]}
        out.append({"instrument": sym, "score": round(score, 3), "n": len(contrib), "conflict": len(dirs) > 1,
                    "contributors": sorted(contrib, key=lambda c: -(c["confidence"] or 0))})
    out.sort(key=lambda x: (-abs(x["score"]), -x["n"]))
    return out
