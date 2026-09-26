#!/usr/bin/env python3
"""Export a cabinet database pair from Atlas into the generator's file layout, so score_preps.py can grade it.

    python export_atlas.py pregamev0_abhi_cabinet20                # -> data/pregamev0_abhi_cabinet20/out/
    python export_atlas.py pregamev0_abhi_cabinet80 --env-file C:/Projects/pregame-viewer/viewer.env
    python score_preps.py --baseline --data data/pregamev0_abhi_cabinet20/out

Reads <db> (the visible side) and <db>_truth (the answer side) and writes
    out/visible/<collection>.json            every visible collection (book_summary and vocabulary as one object)
    out/hidden/truth.json                    {client_id: {attribute: [{since, value}], life_events: [...]}}
    out/hidden/claims.json                   the claims list
    out/hidden/answer_key.json               {"summary": answer_key_summary, "faults": answer_key, "expected": expected_actions}
    out/hidden/splits.json                   [{client_id, split, story}] from client_stories (or the visible splits)
    out/hidden/<other>.json                  any other answer-side collection
Dates come back as "YYYY-MM-DD" strings, as the generator writes them. Read-only on the database.
The connection string comes from MONGODB_URI or --env-file and is never printed.
"""
import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SINGLE = {"book_summary", "vocabulary", "answer_key_summary", "expiry_reference", "expiry_reference_tune"}


def uri_from(env_file):
    if os.environ.get("MONGODB_URI"):
        return os.environ["MONGODB_URI"]
    if env_file:
        for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() == "MONGODB_URI":
                    return v.strip().strip("'\"")
    raise SystemExit("set MONGODB_URI or pass --env-file")


def plain(value):
    """BSON -> the generator's JSON: datetimes at midnight become YYYY-MM-DD, _id is dropped."""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [plain(v) for v in value]
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read(db, name):
    docs = [plain(d) for d in db[name].find({})]
    if name in SINGLE:
        return docs[0] if docs else {}
    return docs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("db", help="the visible database; its answer side is <db>_truth")
    ap.add_argument("--out", help="output folder (default data/<db>/out)")
    ap.add_argument("--env-file", help="a file with MONGODB_URI=... (used when the variable is not set)")
    a = ap.parse_args()
    from pymongo import MongoClient
    client = MongoClient(uri_from(a.env_file), serverSelectionTimeoutMS=10000)
    vis, hid = client[a.db], client[a.db + "_truth"]
    out = Path(a.out) if a.out else HERE / "data" / a.db / "out"
    counts = {}
    for name in sorted(n for n in vis.list_collection_names() if not n.startswith("system.")):
        data = read(vis, name)
        dump(out / "visible" / f"{name}.json", data)
        counts[f"visible/{name}"] = len(data) if isinstance(data, list) else 1
    hidden = {n: read(hid, n) for n in hid.list_collection_names() if not n.startswith("system.")}
    truth = {}
    for doc in hidden.pop("truth", []):
        cid = doc.pop("client_id")
        truth[cid] = doc
    dump(out / "hidden" / "truth.json", truth)
    dump(out / "hidden" / "claims.json", hidden.pop("claims", []))
    dump(out / "hidden" / "answer_key.json", {"summary": hidden.pop("answer_key_summary", {}),
                                               "faults": hidden.pop("answer_key", []),
                                               "expected": hidden.pop("expected_actions", [])})
    stories = hidden.pop("client_stories", None)
    splits = stories if stories else read(vis, "splits") if "splits" in vis.list_collection_names() else None
    if splits:
        dump(out / "hidden" / "splits.json", splits)
    for name, data in hidden.items():
        dump(out / "hidden" / f"{name}.json", data)
    counts.update({"hidden/truth": len(truth)})
    print(json.dumps({"exported": a.db, "to": str(out), "counts": counts}, indent=1))


if __name__ == "__main__":
    main()
