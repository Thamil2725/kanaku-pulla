import argparse
import json
from pathlib import Path

from .engine import Engine
from .extraction import read_document


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="runtime")
    parser.add_argument("--demo-trust", action="store_true", help="Explicitly load synthetic fixture authorizations/coordinates")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run"); run.add_argument("path")
    sub.add_parser("resume")
    sub.add_parser("effects")
    show = sub.add_parser("show"); show.add_argument("case_id")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent / "fixtures"
    kwargs = {}
    if args.demo_trust:
        kwargs = {"mode": "demo", "authorizations": json.loads((root / "authorizations.json").read_text()),
                  "registry": json.loads((root / "registered_locations.json").read_text())}
    engine = Engine(args.data_dir, **kwargs)
    if args.command == "run":
        path = Path(args.path)
        raw = read_document(path.read_bytes(), path.name)
        result = engine.run(engine.store.ingest(raw, path.name))
    elif args.command == "resume":
        result = engine.resume()
    elif args.command == "effects":
        result = engine.sink.list()
    else:
        result = engine.store.audit(args.case_id)
    print(json.dumps(result, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
