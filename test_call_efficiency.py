
import sys, os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from decision_connector import *

calls = []
class CountingClient:
    def system_one(self, state, questions):
        calls.append(next(iter(questions)))
        probs = {d: 0.0 for d in QCDispositionAdapter.DISPOSITIONS}
        probs["rework"] = 0.62
        class A:
            def __init__(s, **kw): s.__dict__.update(kw)
        class R: model = "jev-1.13.0"
        r = R()
        r.answers = {
            "disposition": A(value="rework", probabilities=probs, confidence=0.62),
            "acceptable_to_ship": A(value=0.12, confidence=0.62),
            "judgment_difficulty": A(value=3, confidence=0.62),
            "best_match": A(value="rework", probabilities=probs, confidence=0.62),
            "compatible": A(value=0.12, confidence=0.62),
            "difficulty": A(value=3, confidence=0.62),
        }
        return r
    def close(self): pass

db = str(Path(__file__).resolve().parent / "_count.db")
if os.path.exists(db): os.remove(db)

defect = {"defect_class":"surface_scratch","severity":"cosmetic_minor",
          "part_family":"bracket-A2","detector_confidence":0.87,
          "tolerance_class":"Class-2 surface","customer_cosmetics":"high",
          "rework_cost_usd":12,"scrap_cost_usd":40,"units_short":0,"lot_size":500}

print("=== ONE_SHOT (QC disposition, 6 candidates) ===")
c = DecisionConnector(QCDispositionAdapter(), db_path=db, client=CountingClient())
d = c.evaluate(defect, None)
print(f"  grid rows written : {len(d)}")
print(f"  JEV API CALLS     : {len(calls)}   <-- one call, six rows")
print(f"  routes            : {sorted(set(c.route(x) for x in d))}")
print(f"  winner            : {max(d, key=lambda x: x.noul or 0)}")
c.close()

print()
print("=== PER_CANDIDATE (fitment, 2 candidates) ===")
calls.clear()
db2 = str(Path(__file__).resolve().parent / "_count2.db")
if os.path.exists(db2): os.remove(db2)
veh = {"arku":"CAR-x","piece_type":"Buick Regal","attributes":{"front_rotor_mm":302}}
parts = {"pad-a":{"name":"A"},"pad-b":{"name":"B"}}
c2 = DecisionConnector(CompatalogFitmentAdapter(), db_path=db2, client=CountingClient())
d2 = c2.evaluate(veh, parts)
print(f"  grid rows written : {len(d2)}")
print(f"  JEV API CALLS     : {len(calls)}   <-- two candidates need two judgments")
d2b = c2.evaluate(veh, parts)
print(f"  warm re-run calls : {len(calls)-2}  (sources: {[x.source for x in d2b]})")
c2.close()
os.remove(db); os.remove(db2)
