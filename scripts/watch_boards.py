"""Check the watched job boards for new postings (Scout, slice 1).

Usage:
    python3 scripts/watch_boards.py                      # check all, print new
    python3 scripts/watch_boards.py add "Anthropic" https://job-boards.greenhouse.io/anthropic "research engineer,software engineer"
    python3 scripts/watch_boards.py add "Nvidia" https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite "grad,intern"
    python3 scripts/watch_boards.py list
    python3 scripts/watch_boards.py import boards.csv
    python3 scripts/watch_boards.py --check --resume resume.txt

Watchlist: data/job_boards/watchlist.json. Seen postings: data/job_boards/seen.json.
The daily digest (scripts/daily_digest.py) includes this report automatically.
"""
import argparse
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion import fit
from companion.job_boards import (
    WatchEntry,
    check_boards,
    load_watchlist,
    render_report,
    save_watchlist,
    seed_entry,
    slug_from_url,
)
from companion.logging_setup import configure_logging
from companion.paths import DATA_DIR, write_json
from companion.ready import ReadyStore, plan_re_enqueue, reassess


def _board_rows(entry, fetch_json):
    """Keep raw publication and update fields separate; existing Posting conflates them."""
    from companion.job_posting_fetch import _html_to_text
    token = quote(entry.token, safe="")
    if entry.source == "greenhouse":
        jobs = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")["jobs"]
        for job in jobs:
            yield {"id": str(job["id"]), "url": job["absolute_url"], "title": job["title"],
                   "text": _html_to_text(job.get("content", "")), "location": (job.get("location") or {}).get("name", ""),
                   "published": job.get("first_published"), "updated": job.get("updated_at")}
    elif entry.source == "lever":
        jobs = fetch_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
        for job in jobs:
            parts = [job.get("descriptionPlain", ""), job.get("openingPlain", ""), job.get("additionalPlain", "")]
            parts += [_html_to_text(section.get("content", "")) for section in job.get("lists", [])]
            updated = job.get("updatedAt")
            yield {"id": job["id"], "url": job["hostedUrl"], "title": job["text"], "text": "\n".join(parts),
                   "location": (job.get("categories") or {}).get("location", ""), "published": None,
                   "updated": datetime.fromtimestamp(updated / 1000, UTC).isoformat() if updated else None}
    elif entry.source == "ashby":
        jobs = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")["jobs"]
        for job in jobs:
            if job.get("isListed") is False:
                continue
            yield {"id": job.get("id") or job["jobUrl"], "url": job["jobUrl"], "title": job["title"],
                   "text": job.get("descriptionPlain") or _html_to_text(job.get("descriptionHtml", "")),
                   "location": job.get("location", ""), "published": job.get("publishedAt"), "updated": None}


def boards_due(entries, history, *, starred, now):
    stars = {name.casefold() for name in starred}
    due = []
    for entry in entries:
        last = history.get("boards", {}).get(f"{entry.source}:{entry.token}", {}).get("last_check")
        if entry.company.casefold() in stars or not last:
            due.append(entry)
            continue
        try:
            checked = datetime.fromisoformat(last)
            checked = checked if checked.tzinfo else checked.replace(tzinfo=UTC)
            age = (now - checked).total_seconds()
        except (TypeError, ValueError):
            age = -1  # Bad or future timestamps must not suppress a board indefinitely.
        if age < 0 or age >= 55 * 60:
            due.append(entry)
    return due


def check_ready(*, now=None, **kwargs):
    now = now or datetime.now().astimezone()
    if not 7 <= now.hour < 23:
        return {"skipped": "outside 07:00-23:00", "enqueued": 0, "re_enqueued": 0, "re_assessed": 0, "skipped_on_reassess": 0, "not_due": 0}
    # Serialize manual checks with the scheduled process, separately from the queue lock.
    with ReadyStore(DATA_DIR / "job_boards" / "ready-check.json").locked():
        return _check_ready(now=now, **kwargs)


def _check_ready(*, now=None, fetch_json=None, entries=None, ready=None, applications=None, queue=None):
    """One scheduled check; no browser, no model call, no automatic applied transition."""
    from companion.apply_pipeline import ALREADY_DONE
    from companion.db import engine_for_store
    from companion.job_applications import JobApplicationStore
    from companion.job_boards import _get_json
    from companion.jobs import DbJobQueue
    from companion.scout import board_url

    now = now or datetime.now().astimezone()
    if not 7 <= now.hour < 23:
        return {"skipped": "outside 07:00-23:00", "enqueued": 0, "re_enqueued": 0, "re_assessed": 0, "skipped_on_reassess": 0, "not_due": 0}
    words_path = DATA_DIR / "private_docs" / "fit-keywords.txt"
    if not words_path.exists():
        return {"skipped": "fit keywords not configured", "enqueued": 0, "re_enqueued": 0, "re_assessed": 0, "skipped_on_reassess": 0, "not_due": 0}
    wanted = [line.strip() for line in words_path.read_text().splitlines() if line.strip() and not line.startswith("#")]
    if not wanted:
        return {"skipped": "fit keywords empty", "enqueued": 0, "re_enqueued": 0, "re_assessed": 0, "skipped_on_reassess": 0, "not_due": 0}
    stars_path = DATA_DIR / "job_boards" / "starred.json"
    stars = json.loads(stars_path.read_text()) if stars_path.exists() else []
    # Optional explicit size labels; never infer size from the number of openings.
    sizes_path = DATA_DIR / "job_boards" / "small_companies.json"
    sizes = json.loads(sizes_path.read_text()) if sizes_path.exists() else {}
    if not isinstance(stars, list) or not all(isinstance(name, str) for name in stars):
        raise ValueError("starred.json must be a list of company names")
    if not isinstance(sizes, dict) or not all(isinstance(v, bool) for v in sizes.values()):
        raise ValueError("small_companies.json must map company names to true/false")
    sizes = {name.casefold(): value for name, value in sizes.items()}
    ready = ready or ReadyStore()
    applications = applications or JobApplicationStore()
    queue = queue or DbJobQueue(engine_for_store(DATA_DIR / "kyra.db"))
    fetch_json = fetch_json or _get_json
    entries = load_watchlist() if entries is None else entries
    history_path = DATA_DIR / "job_boards" / "ready-observations.json"
    history = json.loads(history_path.read_text()) if history_path.exists() else {"boards": {}, "postings": {}}
    summary = {"checked": 0, "enqueued": 0, "re_enqueued": 0, "deep": 0, "none_suitable": [], "errors": [],
               "re_assessed": 0, "skipped_on_reassess": 0, "not_due": 0}
    due = boards_due(entries, history, starred=stars, now=now)
    summary["not_due"] = len(entries) - len(due)
    stamp = now.isoformat()
    for entry in due:
        if entry.source not in {"greenhouse", "lever", "ashby"}:
            continue
        board_key = f"{entry.source}:{entry.token}"
        try:
            rows = list(_board_rows(entry, fetch_json))
            board = history["boards"].setdefault(board_key, {"baseline": stamp})
            assessed = []
            for row in rows:
                url = board_url(row["url"])
                if not url and entry.source == "greenhouse":
                    url = f"https://job-boards.greenhouse.io/{quote(entry.token, safe='')}/jobs/{quote(row['id'], safe='')}"
                if not url:
                    continue
                row.update(company=entry.company, url=url)
                prior = history["postings"].get(url)
                dates = {"published": row["published"], "updated": row["updated"],
                         "first_seen": prior["first_seen"] if prior else stamp, "last_check": stamp}
                # Fetch a first-publication date only for new Greenhouse postings.
                if not prior and entry.source == "greenhouse" and not dates["published"]:
                    detail = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{quote(entry.token, safe='')}/jobs/{quote(row['id'], safe='')}")
                    dates.update(published=detail.get("first_published"), updated=detail.get("updated_at"))
                if prior and not dates["published"]:
                    dates["published"] = prior.get("published")
                history["postings"][url] = dates
                verdict = fit.assess(row, wanted=wanted, small_company=sizes.get(entry.company.casefold()))
                age, days, basis = fit.freshness(**{k: dates[k] for k in ("published", "updated", "first_seen")}, now=now, baseline=board["baseline"])
                row["freshness"] = {"state": age, "days": days, "basis": basis, **dates}
                row["deep"] = fit.deep(entry.company, verdict.matched, starred=set(stars))
                # Include retained candidates after a crash, but never repeat a completed queue item.
                assessed.append((row, verdict))
            board["last_check"] = stamp
            summary["checked"] += 1
            write_json(history_path, history)
            if assessed and all(v.verdict == "unsuitable" for _, v in assessed):
                summary["none_suitable"].append(entry.company)
            queued_urls = {item["url"] for item in ready.list()}
            existing_apps = {app.link: app for app in applications.list()}
            eligible = [(row, verdict) for row, verdict in assessed
                        if verdict.verdict in {"strong", "stretch"} and row["url"] not in queued_urls
                        and (row["deep"] or row["freshness"]["state"] in {"fresh", "unknown"})
                        and not (row["url"] in existing_apps and
                                 existing_apps[row["url"]].status in ALREADY_DONE | {"ready_to_submit"})]
            choice = fit.best_per_company(eligible).get(entry.company)
            if choice is None:
                continue
            verdict = next(v for row, v in eligible if row is choice)
            existing_app = existing_apps.get(choice["url"])
            app = existing_app or applications.add(entry.company, choice["title"], link=choice["url"], status="targeting")
            item = ready.add(application_id=app.id, url=choice["url"], company=entry.company, role=choice["title"],
                             fit=asdict(verdict), freshness=choice["freshness"], deep=choice["deep"],
                             text=choice["text"], location=choice["location"])
            if choice["deep"]:
                ready.mark(item["id"], "needs_input", questions=["Deep review: prepare deliberately in APPLY."])
                summary["deep"] += 1
                continue
            ready.mark(item["id"], "needs_input", questions=["Documents queued for preparation."])
            _, created = ready.enqueue_preparation(item["id"], queue, posting_text=choice["text"])
            summary["enqueued"] += int(created)
        except Exception as exc:  # A failed board cannot suppress the others or mean zero jobs.
            summary["errors"].append({"board": board_key, "error": type(exc).__name__})
    summary.update(reassess(ready, wanted=wanted, starred=set(stars)))
    by_id = {item["id"]: item for item in ready.list()}
    active = {}
    for job in queue.list(limit=sys.maxsize):
        if job.kind == "prepare" and job.status in {"queued", "running"}:
            application_id = job.payload.get("application_id") or by_id.get(job.payload.get("ready_id"), {}).get("application_id")
            if application_id is not None:
                active[application_id] = job.status
    todo = set(plan_re_enqueue(ready, active_prepare_jobs=active, limit=5))
    for item in ready.needing_preparation():
        if item["application_id"] not in todo:
            continue
        try:
            _, created = ready.enqueue_preparation(item["id"], queue)
            summary["re_enqueued"] += int(created)
        except Exception as exc:
            summary["errors"].append({"item": item["id"], "error": type(exc).__name__})
    return summary


def _backfill_watcher(entries, seen_path, *, sources=None):
    """Migrate missing boards only; later digest observations are independent."""
    watcher_seen = seen_path.parent / 'watcher-seen.json'
    legacy = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    baseline = json.loads(watcher_seen.read_text()) if watcher_seen.exists() else dict(legacy)
    initialized = {(value.get('source'), value.get('company')) for value in baseline.values()}
    missing = []
    for entry in entries:
        identity = (entry.source, entry.company)
        if identity in initialized:
            continue
        prior = {key: value for key, value in legacy.items()
                 if (value.get('source'), value.get('company')) == identity}
        if prior:
            for key, value in prior.items():
                baseline.setdefault(key, value)
        else:
            missing.append(entry)
    write_json(watcher_seen, baseline)
    failed, errors = set(), []
    for entry in missing:
        report = seed_entry(entry, seen_path=watcher_seen, sources=sources)
        if report.errors:
            # Never classify an unbaselined board's existing postings as new.
            failed.add((entry.source, entry.token))
            errors.extend(report.errors)
    return watcher_seen, failed, errors


def _seed_watcher(entry, *, seen_path=None, sources=None):
    from companion.job_boards import SEEN_PATH

    seen_path = seen_path or SEEN_PATH
    report = seed_entry(entry, seen_path=seen_path, sources=sources)
    if not report.errors:
        _, _, errors = _backfill_watcher([entry], seen_path, sources=sources)
        report.errors.extend(errors)
    return report


def import_boards(csv_path, *, watchlist_path=None, seen_path=None, sources=None):
    """CSV columns: company,url,keywords (optional, comma-separated in quotes).

    Seed before saving each new board. Failed seeds are not installed, so a later
    scheduled check cannot mislabel the entire existing board as new.
    """
    import csv

    from companion.job_boards import SEEN_PATH, WATCHLIST_PATH

    watchlist_path = watchlist_path or WATCHLIST_PATH
    seen_path = seen_path or SEEN_PATH
    with ReadyStore(seen_path.parent / 'watch-check.json').locked():
        entries = load_watchlist(watchlist_path)
        _, _, errors = _backfill_watcher(entries, seen_path, sources=sources)
        known = {(e.source, e.token) for e in entries}
        result = {'imported': 0, 'skipped': 0, 'errors': errors}
        with Path(csv_path).open(newline='', encoding='utf-8-sig') as stream:
            reader = csv.DictReader(stream)
            if not {'company', 'url'} <= set(reader.fieldnames or []):
                raise ValueError('CSV needs company,url columns; keywords is optional')
            for number, row in enumerate(reader, 2):
                parsed = slug_from_url(row.get('url') or '')
                company = (row.get('company') or '').strip()
                if not parsed or not company:
                    result['errors'].append(f'row {number}: company and supported board URL required')
                    continue
                if parsed in known:
                    result['skipped'] += 1
                    continue
                entry = WatchEntry(company, *parsed, [k.strip() for k in (row.get('keywords') or '').split(',') if k.strip()])
                report = _seed_watcher(entry, seen_path=seen_path, sources=sources)
                if report.errors:
                    result['errors'].extend(report.errors)
                    continue
                entries.append(entry)
                save_watchlist(entries, watchlist_path)
                known.add(parsed)
                result['imported'] += 1
        return result


def watch_hits(*, resume_text, data_dir=DATA_DIR, entries=None, sources=None, applications=None, ready=None, queue=None):
    """Journal new hits then enqueue existing prepare-only pipeline work.

    No model or browser runs here. Targeting/queued is not a claim that a tailored
    draft is prepared; the existing worker owns that transition.
    """
    from companion.apply_pipeline import ALREADY_DONE
    from companion.db import engine_for_store
    from companion.job_applications import JobApplicationStore
    from companion.job_match import match_score, urgency
    from companion.job_posting_fetch import TargetPostingTool
    from companion.jobs import DbJobQueue
    from companion.outreach import OutreachStore, draft_for_job_hit

    folder = Path(data_dir) / 'job_boards'
    with ReadyStore(folder / 'watch-check.json').locked():
        hits_path = folder / 'hits.jsonl'
        hits = [json.loads(line) for line in hits_path.read_text().splitlines() if line.strip()] if hits_path.exists() else []
        known = {hit['key'] for hit in hits}

        def save_hits():
            # Atomic replacement avoids truncated JSONL records after interruption.
            temporary = hits_path.with_suffix('.jsonl.tmp')
            temporary.write_text(''.join(json.dumps(hit) + '\n' for hit in hits), encoding='utf-8')
            temporary.replace(hits_path)

        def record(posting):
            if posting.key in known:
                return
            score = match_score(posting, resume_text)
            hits.append({**asdict(posting), 'key': posting.key, 'score': score, 'urgency': urgency(score),
                         'first_seen': datetime.now(UTC).isoformat(), 'delivery': 'pending'})
            save_hits()
            known.add(posting.key)

        entries = load_watchlist(folder / 'watchlist.json') if entries is None else entries
        watcher_seen, failed, errors = _backfill_watcher(entries, folder / 'seen.json', sources=sources)
        eligible = [entry for entry in entries if (entry.source, entry.token) not in failed]
        report = check_boards(eligible, watcher_seen, sources, on_new=record)
        report.errors.extend(errors)
        applications = applications if applications is not None else JobApplicationStore(path=Path(data_dir) / 'kyra.db')
        ready = ready if ready is not None else ReadyStore(folder / 'ready.json')
        queue = queue if queue is not None else DbJobQueue(engine_for_store(Path(data_dir) / 'kyra.db'))
        result = {'new': len(report.new), 'enqueued': 0, 'errors': list(report.errors)}
        for hit in hits:
            if hit['delivery'] != 'pending':
                continue
            try:
                # The pipeline's targeting step, using already fetched public text.
                target = TargetPostingTool(applications, fetch=lambda url: None).run(
                    url=hit['url'], posting_text=hit['text'].strip() or hit['title'], company=hit['company'], role=hit['title'])
                if 'error' in target:
                    raise ValueError(target['error'])
                app = target['application']
                hit['application_id'] = app['id']
                if app['status'] in ALREADY_DONE | {'prepared', 'ready_to_submit'}:
                    hit['delivery'] = 'already_tracked'
                else:
                    item = ready.add(application_id=app['id'], url=hit['url'], company=hit['company'], role=hit['title'],
                                     fit={'score': hit['score'], 'urgency': hit['urgency'],
                                          'verdict': 'strong' if hit['score'] >= 70 else 'stretch',
                                          'reasons': [f"Local match score: {hit['score']}/100 (heuristic)"]},
                                     freshness={'state': 'unknown', 'first_seen': hit['first_seen']}, deep=False,
                                     text=hit['text'], location=hit['location'])
                    if item['state'] in {'applied', 'skipped', 'ready'} or item['resume_path']:
                        hit['delivery'] = 'already_tracked'
                    else:
                        ready.mark(item['id'], 'needs_input', questions=['Documents queued for preparation.'])
                        _, created = ready.enqueue_preparation(item['id'], queue, posting_text=hit['text'])
                        result['enqueued'] += int(created)
                        hit['delivery'] = 'queued'
                save_hits()
            except Exception as exc:  # One failed handoff must not suppress other hits; retry next run.
                hit['delivery'] = 'pending'
                result['errors'].append({'key': hit['key'], 'error': type(exc).__name__})
        # Retry independently of application preparation; an outreach failure must not undo queued work.
        contacts = OutreachStore(Path(data_dir) / 'outreach.db')
        for hit in hits:
            if hit.get('application_id'):
                try:
                    draft_for_job_hit(contacts, applications, hit['application_id'],
                                      company_kind=hit.get('company_kind'), public_email=hit.get('public_email'))
                except Exception as exc:
                    result['errors'].append({'key': hit['key'], 'error': type(exc).__name__})
        from companion.phone_link import LinkRefused, notify_job_hits

        try:
            result['phone_notice'] = notify_job_hits(data_dir)
        except (LinkRefused, OSError) as exc:
            result['errors'].append({'stage': 'phone_notice', 'error': type(exc).__name__})
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="assess approved boards and enqueue preparation")
    parser.add_argument("--resume", type=Path, help="local resume text/TeX for deterministic scoring")
    sub = parser.add_subparsers(dest="cmd")
    add = sub.add_parser("add", help="add a company board to the watchlist")
    add.add_argument("company")
    add.add_argument("url", help="a Greenhouse, Lever, Ashby or Workday board/posting URL")
    add.add_argument("keywords", nargs="?", default="", help="comma-separated title keywords (empty = engineering role families)")
    sub.add_parser("list")
    imp = sub.add_parser("import", help="seed boards from CSV: company,url,keywords")
    imp.add_argument("csv", type=Path)
    args = parser.parse_args()
    configure_logging()

    if args.check:
        if args.cmd:
            parser.error("--check cannot be combined with a subcommand")
        resume_path = args.resume
        if resume_path is None:
            from companion.settings import Settings
            resume_path = Path(Settings().resume_base_tex)
            if not resume_path.is_absolute():
                resume_path = DATA_DIR / resume_path
        if not resume_path.is_file():
            parser.error("resume text missing; configure KYRA_RESUME_BASE_TEX or pass --resume")
        print(json.dumps(watch_hits(resume_text=resume_path.read_text(encoding="utf-8"))))
        return
    if args.cmd == "import":
        print(json.dumps(import_boards(args.csv)))
        return
    entries = load_watchlist()
    if args.cmd == "add":
        parsed = slug_from_url(args.url)
        if not parsed:
            sys.exit("couldn't recognise a Greenhouse, Lever, Ashby or Workday board in that URL")
        source, token = parsed
        kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
        entries = [e for e in entries if not (e.source == source and e.token == token)]
        entry = WatchEntry(company=args.company, source=source, token=token, title_keywords=kws)
        entries.append(entry)
        save_watchlist(entries)
        print(f"watching {args.company} ({source}/{token}) keywords={kws or 'engineering families'}")
        # Check it now, so what is already open is shown here rather than arriving
        # in tomorrow's digest as if it appeared overnight.
        from companion.job_boards import SEEN_PATH
        with ReadyStore(SEEN_PATH.parent / 'watch-check.json').locked():
            _, _, errors = _backfill_watcher(entries, SEEN_PATH)
            report = _seed_watcher(entry)
            report.errors.extend(errors)
        print()
        print(render_report(report))
        if not report.errors:
            print("\n(recorded as already seen - from now on the digest reports only what is new)")
        return
    if args.cmd == "list":
        for e in entries:
            print(f"  {e.company:24s} {e.source}/{e.token:20s} {', '.join(e.title_keywords) or '(engineering families)'}")
        if not entries:
            print("  (empty - add one with: watch_boards.py add <Company> <board url> [keywords])")
        return
    if not entries:
        print("watchlist is empty")
        return
    report = check_boards(entries)
    print(render_report(report))


if __name__ == "__main__":
    main()
