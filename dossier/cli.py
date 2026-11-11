"""Command line entry point: `python -m dossier.cli ...`."""
from __future__ import annotations

import argparse
import sys

from . import config, db


def _init() -> None:
    db.init_db()


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("dossier.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_worker(args) -> int:
    from .pipeline import orchestrator

    _init()
    recovered = orchestrator.recover_stale_jobs()
    print(f"worker {orchestrator.WORKER_ID} · mode={config.RUN_MODE} · recovered={recovered}")
    orchestrator.worker_loop(once=args.once)
    return 0


def cmd_eval(args) -> int:
    from . import evals

    _init()
    result = evals.evaluate(
        deal_id=args.deal,
        min_trust=args.min_trust,
        min_coverage=args.min_coverage,
        max_conflicts=args.max_conflicts,
        min_words=args.min_words,
    )
    print(evals.render(result))
    return 0 if result["passed"] else 1



def cmd_new(args) -> int:
    from .app import _unique_deal_id

    _init()
    ident = args.identifier
    deal_id = _unique_deal_id(args.name or ident)
    now = db.now()
    db.insert(
        "deals",
        {
            "id": deal_id,
            "name": args.name or ident,
            "company": None,
            "ticker": None,
            "cik": None,
            "deal_type": args.deal_type,
            "sector": args.sector or "",
            "client": args.client or "",
            "status": "draft",
            "created_at": now,
            "updated_at": now,
            "config": db.dumps({"identifier": ident}),
            "summary": "{}",
        },
    )
    print(deal_id)
    if args.run:
        return cmd_run(args, deal_id=deal_id)
    return 0


def cmd_run(args, deal_id: str | None = None) -> int:
    from .pipeline import orchestrator

    _init()
    deal_id = deal_id or args.deal_id
    row = db.q1("SELECT name FROM deals WHERE id = ?", (deal_id,))
    if not row:
        print(f"No deal {deal_id}", file=sys.stderr)
        return 2
    print(f"Running due diligence for {deal_id} ({row['name']})…")
    try:
        run_id = orchestrator.run_sync(deal_id, polish=args.polish)
    except orchestrator.RunBusy:
        print("A run is already in progress for this deal", file=sys.stderr)
        return 1
    run = orchestrator.run_status(run_id)
    stats = run.get("stats") or {}
    print(f"Run {run_id}: {run['status']}")
    if run.get("error"):
        print(run["error"], file=sys.stderr)
        return 1
    print(
        f"  sources={stats.get('sources')} evidence={stats.get('evidence')} risks={stats.get('risks')} "
        f"words={stats.get('words')} trust={stats.get('trust')} coverage={stats.get('coverage')}%"
    )
    return 0


def cmd_demo(args) -> int:
    from . import app as app_mod

    _init()
    existing = db.q1("SELECT id FROM deals WHERE id = ?", ("lovesac",))
    if not existing:
        now = db.now()
        db.insert(
            "deals",
            {
                "id": "lovesac",
                "name": "Lovesac",
                "company": None,
                "ticker": None,
                "cik": None,
                "deal_type": "sell_side",
                "sector": "Consumer / furniture",
                "client": "Confidential — demonstration",
                "status": "draft",
                "created_at": now,
                "updated_at": now,
                "config": db.dumps({"identifier": "LOVE", "demo": True}),
                "summary": "{}",
            },
        )
    return cmd_run(args, deal_id="lovesac")


def cmd_export(args) -> int:
    from pathlib import Path

    from . import export

    _init()
    fn = {"docx": export.to_docx, "pdf": export.to_pdf, "xlsx": export.to_xlsx}[args.format]
    data, name = fn(args.deal_id)
    out = Path(args.out or ".") / name
    out.write_bytes(data)
    print(out)
    return 0


def cmd_status(args) -> int:
    _init()
    for d in db.q("SELECT id, name, company, ticker, status, updated_at FROM deals ORDER BY updated_at DESC"):
        counts = db.q1(
            "SELECT (SELECT COUNT(*) FROM sources WHERE deal_id = ?) s, "
            "(SELECT COUNT(*) FROM evidence WHERE deal_id = ?) e, "
            "(SELECT COUNT(*) FROM risks WHERE deal_id = ?) r",
            (d["id"], d["id"], d["id"]),
        )
        print(f"{d['id']:<22} {str(d['company'] or d['name']):<26} {d['status']:<12} "
              f"{counts['s']:>3} sources {counts['e']:>4} evidence {counts['r']:>3} risks")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dossier", description=config.APP_TAGLINE)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the web application")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    n = sub.add_parser("new", help="Create a deal from a ticker, name or CIK")
    n.add_argument("identifier")
    n.add_argument("--name")
    n.add_argument("--client", default="")
    n.add_argument("--sector", default="")
    n.add_argument("--deal-type", default="sell_side")
    n.add_argument("--run", action="store_true")
    n.add_argument("--polish", dest="polish", action="store_true", default=None,
                   help="Force model polish (default: use the configured provider)")
    n.add_argument("--no-polish", dest="polish", action="store_false",
                   help="Force the deterministic engine")
    n.set_defaults(func=cmd_new)

    r = sub.add_parser("run", help="Run the pipeline for a deal")
    r.add_argument("deal_id")
    r.add_argument("--polish", dest="polish", action="store_true", default=None,
                   help="Force model polish (default: use the configured provider)")
    r.add_argument("--no-polish", dest="polish", action="store_false",
                   help="Force the deterministic engine")
    r.set_defaults(func=cmd_run)

    d = sub.add_parser("demo", help="Create and run the Lovesac demonstration deal")
    d.add_argument("--polish", dest="polish", action="store_true", default=None,
                   help="Force model polish (default: use the configured provider)")
    d.add_argument("--no-polish", dest="polish", action="store_false",
                   help="Force the deterministic engine")
    d.set_defaults(func=cmd_demo)

    e = sub.add_parser("export", help="Export the latest document")
    e.add_argument("deal_id")
    e.add_argument("format", choices=["docx", "pdf", "xlsx"])
    e.add_argument("--out")
    e.set_defaults(func=cmd_export)

    st = sub.add_parser("status", help="List deals and artifact counts")
    st.set_defaults(func=cmd_status)

    w = sub.add_parser("worker", help="Run the durable job worker")
    w.add_argument("--once", action="store_true", help="Process at most one job and exit")
    w.set_defaults(func=cmd_worker)

    ev = sub.add_parser("eval", help="Score generated documents against quality gates")
    ev.add_argument("--deal")
    ev.add_argument("--min-trust", type=int, default=95)
    ev.add_argument("--min-coverage", type=float, default=90.0)
    ev.add_argument("--max-conflicts", type=int, default=0)
    ev.add_argument("--min-words", type=int, default=1500)
    ev.set_defaults(func=cmd_eval)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
