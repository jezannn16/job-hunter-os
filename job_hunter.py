"""Local-first job application tracker and evidence-based skills matcher."""

from __future__ import annotations

import argparse
import csv
import email.utils
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

STATUSES = ("saved", "applied", "screening", "interview", "offer", "rejected", "withdrawn")
STATUS_TRANSITIONS = {
    "saved": {"applied", "withdrawn"},
    "applied": {"screening", "interview", "offer", "rejected", "withdrawn"},
    "screening": {"interview", "offer", "rejected", "withdrawn"},
    "interview": {"interview", "offer", "rejected", "withdrawn"},
    "offer": {"rejected", "withdrawn"},
    "rejected": set(),
    "withdrawn": set(),
}
SKILL_ALIASES = {
    "python": ("python", "питон"),
    "sql": ("sql",),
    "fastapi": ("fastapi",),
    "postgresql": ("postgresql", "postgres", "постгрес"),
    "docker": ("docker",),
    "airflow": ("airflow",),
    "python typing": ("type hints", "типизация"),
    "data analysis": ("data analysis", "анализ данных", "аналитика данных"),
    "machine learning": ("machine learning", "машинное обучение", "ml"),
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def spreadsheet_safe_text(value: object) -> object:
    """Neutralize spreadsheet formulas in untrusted text written to CSV."""
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def connect(db_path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(db_path)
    if os.name == "posix" and str(db_path) != ":memory:":
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            db.close()
            raise RuntimeError("Could not restrict the database file to the current user") from None
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
    CREATE TABLE IF NOT EXISTS vacancies(id INTEGER PRIMARY KEY, company_id INTEGER REFERENCES companies(id), title TEXT NOT NULL,
      url TEXT, description TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual', fetched_at TEXT, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS applications(id INTEGER PRIMARY KEY, vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
      status TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS status_history(id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
      old_status TEXT, new_status TEXT NOT NULL, changed_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS resume_versions(id INTEGER PRIMARY KEY, name TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS interview_events(id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
      starts_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS interview_questions(id INTEGER PRIMARY KEY, interview_id INTEGER NOT NULL REFERENCES interview_events(id) ON DELETE CASCADE,
      question TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS interview_post_notes(id INTEGER PRIMARY KEY, interview_id INTEGER NOT NULL REFERENCES interview_events(id) ON DELETE CASCADE,
      body TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS follow_up_tasks(id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
      due_at TEXT NOT NULL, note TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS follow_up_reminders(task_id INTEGER NOT NULL REFERENCES follow_up_tasks(id) ON DELETE CASCADE,
      reminder_date TEXT NOT NULL, recorded_at TEXT NOT NULL, PRIMARY KEY(task_id,reminder_date));
    CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
      body TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_vacancy_title ON vacancies(title);
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(vacancies)")}
    if "published_at" not in columns:
        db.execute("ALTER TABLE vacancies ADD COLUMN published_at TEXT")
    return db


def add_vacancy(
    db: sqlite3.Connection, title: str, company: str, url: str = "", description: str = "", source: str = "manual",
    published_at: str = "",
) -> int:
    title, company = title.strip(), company.strip()
    if not title or not company:
        raise ValueError("Title and company are required")
    cur = db.execute(
        "INSERT INTO companies(name) VALUES(?) ON CONFLICT(name) DO UPDATE SET name=excluded.name", (company,)
    )
    company_id = db.execute("SELECT id FROM companies WHERE name=?", (company,)).fetchone()[0]
    cur = db.execute(
        "INSERT INTO vacancies(company_id,title,url,description,source,fetched_at,created_at,published_at) VALUES(?,?,?,?,?,?,?,?)",
        (company_id, title, url.strip(), description.strip(), source, now() if source != "manual" else None, now(), published_at or None),
    )
    vacancy_id = cur.lastrowid
    cur = db.execute(
        "INSERT INTO applications(vacancy_id,status,updated_at) VALUES(?,?,?)", (vacancy_id, "saved", now())
    )
    db.execute(
        "INSERT INTO status_history(application_id,old_status,new_status,changed_at) VALUES(?,?,?,?)",
        (cur.lastrowid, None, "saved", now()),
    )
    return int(vacancy_id)


def set_status(db: sqlite3.Connection, vacancy_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"Status must be one of: {', '.join(STATUSES)}")
    app = db.execute("SELECT id,status FROM applications WHERE vacancy_id=?", (vacancy_id,)).fetchone()
    if app is None:
        raise ValueError("Vacancy not found")
    if app["status"] == status:
        return
    if status not in STATUS_TRANSITIONS[app["status"]]:
        raise ValueError(f"Cannot move application from {app['status']} to {status}")
    stamp = now()
    db.execute("UPDATE applications SET status=?,updated_at=? WHERE id=?", (status, stamp, app["id"]))
    db.execute(
        "INSERT INTO status_history(application_id,old_status,new_status,changed_at) VALUES(?,?,?,?)",
        (app["id"], app["status"], status, stamp),
    )


def due_reminders(db: sqlite3.Connection, at: datetime | None = None, record: bool = False) -> list[dict[str, object]]:
    """Return due follow-ups; optional daily recording makes repeated scheduler runs idempotent."""
    now_at = at or datetime.now(UTC)
    if now_at.utcoffset() is None:
        raise ValueError("Reminder time must include a timezone")
    rows = db.execute(
        """SELECT f.id,f.due_at,f.note,v.title,c.name company
           FROM follow_up_tasks f JOIN applications a ON a.id=f.application_id
           JOIN vacancies v ON v.id=a.vacancy_id JOIN companies c ON c.id=v.company_id
           WHERE f.completed=0 ORDER BY f.due_at,f.id"""
    ).fetchall()
    reminders = []
    for row in rows:
        due_at = datetime.fromisoformat(row["due_at"])
        if due_at.utcoffset() is None:
            raise ValueError(f"Follow-up #{row['id']} has no timezone; update it with an ISO datetime offset")
        local_now = now_at.astimezone(due_at.tzinfo)
        if due_at > local_now:
            continue
        reminder_date = local_now.date().isoformat()
        key = (row["id"], reminder_date)
        if db.execute(
            "SELECT 1 FROM follow_up_reminders WHERE task_id=? AND reminder_date=?", key
        ).fetchone():
            continue
        reminders.append(
            {
                "id": row["id"], "due_at": due_at.isoformat(), "note": row["note"],
                "title": row["title"], "company": row["company"],
            }
        )
        if record:
            db.execute(
                "INSERT OR IGNORE INTO follow_up_reminders(task_id,reminder_date,recorded_at) VALUES(?,?,?)",
                (row["id"], reminder_date, now_at.astimezone(UTC).isoformat(timespec="seconds")),
            )
    reminders.sort(key=lambda row: datetime.fromisoformat(str(row["due_at"])).astimezone(UTC))
    return reminders


def complete_followup(db: sqlite3.Connection, task_id: int) -> None:
    changed = db.execute("UPDATE follow_up_tasks SET completed=1 WHERE id=? AND completed=0", (task_id,)).rowcount
    if not changed:
        raise ValueError("Pending follow-up not found")


def add_interview_question(db: sqlite3.Connection, interview_id: int, question: str) -> None:
    question = question.strip()
    if not question:
        raise ValueError("Interview question is required")
    if db.execute("SELECT 1 FROM interview_events WHERE id=?", (interview_id,)).fetchone() is None:
        raise ValueError("Interview not found")
    db.execute("INSERT INTO interview_questions(interview_id,question,created_at) VALUES(?,?,?)", (interview_id, question, now()))


def add_interview_note(db: sqlite3.Connection, interview_id: int, body: str) -> None:
    body = body.strip()
    if not body:
        raise ValueError("Interview note is required")
    if db.execute("SELECT 1 FROM interview_events WHERE id=?", (interview_id,)).fetchone() is None:
        raise ValueError("Interview not found")
    db.execute("INSERT INTO interview_post_notes(interview_id,body,created_at) VALUES(?,?,?)", (interview_id, body, now()))


def list_vacancies(db: sqlite3.Connection, query: str = "", status: str = "") -> list[sqlite3.Row]:
    sql = """SELECT v.id,v.title,c.name company,v.url,v.source,v.fetched_at,v.published_at,a.status,v.created_at,v.description
             FROM vacancies v JOIN companies c ON c.id=v.company_id JOIN applications a ON a.vacancy_id=v.id"""
    clauses, params = [], []
    if query:
        clauses.append("(v.title LIKE ? OR c.name LIKE ? OR v.description LIKE ?)")
        params.extend([f"%{query}%"] * 3)
    if status:
        clauses.append("a.status=?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return list(db.execute(sql + " ORDER BY v.created_at DESC", params))


def match_resume(description: str, resume: str) -> dict[str, object]:
    """Only reports evidence found in the resume; absence means unknown, never a negative claim."""
    desc, cv = description.casefold(), resume.casefold()
    required = re.findall(r"(?:required|обязательно|требуется)\s*:?\s*([^.;\n]+)", desc)
    desired = re.findall(r"(?:preferred|желательно)\s*:?\s*([^.;\n]+)", desc)
    terms = set()
    for chunk in required + desired:
        for term, aliases in SKILL_ALIASES.items():
            if any(a in chunk.casefold() for a in aliases):
                terms.add(term)
    if not terms:
        terms.update(term for term, aliases in SKILL_ALIASES.items() if any(a in desc for a in aliases))
    evidence = {}
    for term in sorted(terms):
        hits = [a for a in SKILL_ALIASES[term] if a in cv]
        if hits:
            needle = hits[0]
            pos = cv.find(needle)
            evidence[term] = (
                resume[max(0, pos - 50) : min(len(resume), pos + len(needle) + 80)].replace("\n", " ").strip()
            )
    return {
        "matched": evidence,
        "unmentioned": sorted(terms - set(evidence)),
        "score": round(100 * len(evidence) / len(terms)) if terms else None,
        "score_note": "Heuristic overlap only; not a probability of getting an offer.",
    }


class VacancySource(Protocol):
    def search(self, text: str, page: int = 0) -> list[dict[str, str]]: ...


@dataclass
class HHSource:
    """Read-only adapter for the public HH.ru vacancy search API."""

    user_agent: str = "PersonalAutomationLab/0.1 (local personal tool)"
    timeout: float = 8.0
    cache_seconds: float = 300.0
    max_attempts: int = 3
    backoff_seconds: float = 0.25
    _cache: dict[tuple[str, int], tuple[float, list[dict[str, str]]]] = field(default_factory=dict, repr=False)

    def search(self, text: str, page: int = 0) -> list[dict[str, str]]:
        if not text.strip() or page < 0:
            raise ValueError("Search text and a non-negative page are required")
        key = (text.strip(), page)
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return [dict(row) for row in cached[1]]
        query = urllib.parse.urlencode({"text": text, "page": page, "per_page": 20})
        req = urllib.request.Request(
            f"https://api.hh.ru/vacancies?{query}",
            headers={"User-Agent": self.user_agent, "Accept": "application/json"},
        )
        try:
            payload = self._request_json(req)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, TypeError) as exc:
            raise RuntimeError("HH.ru search failed; check connectivity and retry later") from exc
        items = payload.get("items")
        if not isinstance(items, list):
            raise TypeError("HH.ru returned an invalid vacancy list; retry later")
        fetched_at = now()
        results = []
        for item in items:
            if not isinstance(item, dict):
                raise TypeError("HH.ru returned an invalid vacancy record; retry later")
            employer = item.get("employer") or {}
            if not isinstance(employer, dict):
                raise TypeError("HH.ru returned an invalid employer record; retry later")
            results.append(
                {
                    "title": str(item.get("name") or ""),
                    "company": str(employer.get("name") or "Unknown employer"),
                    "url": str(item.get("alternate_url") or ""),
                    "description": "",
                    "source": "hh.ru",
                    "published_at": str(item.get("published_at") or ""),
                    "fetched_at": fetched_at,
                }
            )
        self._cache[key] = (time.monotonic() + self.cache_seconds, results)
        return [dict(row) for row in results]

    def _request_json(self, req: urllib.request.Request) -> dict[str, object]:
        attempts = max(1, self.max_attempts)
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    payload = json.loads(response.read(2_000_001))
                if not isinstance(payload, dict):
                    raise TypeError("HH.ru returned an invalid response")
                return payload
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if not retryable or attempt + 1 >= attempts:
                    raise
                delay = self._retry_delay(attempt, retry_after)
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt + 1 >= attempts:
                    raise
                delay = self._retry_delay(attempt, None)
            time.sleep(delay)
        raise RuntimeError("HH.ru request attempts exhausted")

    def _retry_delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(30.0, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    target = email.utils.parsedate_to_datetime(retry_after)
                    return min(30.0, max(0.0, (target - datetime.now(target.tzinfo or UTC)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    pass
        base = max(0.0, self.backoff_seconds) * (2**attempt)
        return min(30.0, base * random.uniform(0.8, 1.2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Local job application tracker")
    parser.add_argument("--db", default="job_hunter.sqlite3")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("title")
    p.add_argument("company")
    p.add_argument("--url", default="")
    p.add_argument("--description", default="")
    p = sub.add_parser("list")
    p.add_argument("--search", default="")
    p.add_argument("--status", choices=STATUSES, default="")
    p = sub.add_parser("status")
    p.add_argument("id", type=int)
    p.add_argument("value", choices=STATUSES)
    p = sub.add_parser("import-text")
    p.add_argument("file")
    p.add_argument("--company", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--url", default="")
    p = sub.add_parser("match")
    p.add_argument("id", type=int)
    p.add_argument("resume_file")
    p = sub.add_parser("hh-search")
    p.add_argument("query")
    p.add_argument("--page", type=int, default=0)
    p.add_argument("--save", action="store_true")
    p = sub.add_parser("resume-add")
    p.add_argument("name")
    p.add_argument("file")
    p = sub.add_parser("resume-list")
    p = sub.add_parser("resume-delete")
    p.add_argument("id", type=int)
    p = sub.add_parser("interview-add")
    p.add_argument("vacancy_id", type=int)
    p.add_argument("starts_at")
    p.add_argument("--notes", default="")
    p = sub.add_parser("interview-question-add")
    p.add_argument("interview_id", type=int)
    p.add_argument("question")
    p = sub.add_parser("interview-note-add")
    p.add_argument("interview_id", type=int)
    p.add_argument("body")
    sub.add_parser("interview-questions")
    sub.add_parser("interview-notes")
    p = sub.add_parser("followup-add")
    p.add_argument("vacancy_id", type=int)
    p.add_argument("due_at")
    p.add_argument("note")
    sub.add_parser("followups")
    p = sub.add_parser("reminders", help="List due follow-ups; --record enables once-per-local-day deduplication")
    p.add_argument("--record", action="store_true")
    p = sub.add_parser("followup-complete")
    p.add_argument("id", type=int)
    sub.add_parser("interviews")
    p = sub.add_parser("backup")
    p.add_argument("file")
    p = sub.add_parser("note-add")
    p.add_argument("vacancy_id", type=int)
    p.add_argument("body")
    p = sub.add_parser("dashboard")
    sub.add_parser("export")
    sub.add_parser("export-md")
    p = sub.add_parser("delete")
    p.add_argument("id", type=int)
    sub.add_parser("delete-all")
    args = parser.parse_args()
    db = connect(args.db)
    try:
        if args.cmd == "add":
            print(f"Saved vacancy #{add_vacancy(db, args.title, args.company, args.url, args.description)}")
        elif args.cmd == "list":
            for x in list_vacancies(db, args.search, args.status):
                print(
                    f"#{x['id']} [{x['status']}] {x['title']} — {x['company']} | {x['url']} "
                    f"[{x['source']}; published {x['published_at'] or 'unknown'}; fetched {x['fetched_at'] or 'n/a'}]"
                )
        elif args.cmd == "status":
            set_status(db, args.id, args.value)
            print("Status updated")
        elif args.cmd == "import-text":
            desc = Path(args.file).read_text(encoding="utf-8")
            print(f"Imported vacancy #{add_vacancy(db, args.title, args.company, args.url, desc)}")
        elif args.cmd == "match":
            row = db.execute("SELECT description FROM vacancies WHERE id=?", (args.id,)).fetchone()
            if not row:
                raise ValueError("Vacancy not found")
            print(
                json.dumps(
                    match_resume(row[0], Path(args.resume_file).read_text(encoding="utf-8")),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.cmd == "hh-search":
            results = HHSource().search(args.query, args.page)
            for i, x in enumerate(results, 1):
                print(
                    f"{i}. {x['title']} — {x['company']} | {x['url']} "
                    f"[published {x['published_at'] or 'unknown'}; fetched {x['fetched_at']}]"
                )
                if args.save:
                        add_vacancy(
                            db, x["title"], x["company"], x["url"], x["description"], x["source"], x["published_at"]
                        )
        elif args.cmd == "resume-add":
            content = Path(args.file).read_text(encoding="utf-8")
            if not content.strip():
                raise ValueError("Resume file is empty")
            db.execute(
                "INSERT INTO resume_versions(name,content,created_at) VALUES(?,?,?)", (args.name, content, now())
            )
            print("Resume version saved")
        elif args.cmd == "resume-list":
            for x in db.execute("SELECT id,name,created_at FROM resume_versions ORDER BY id DESC"):
                print(f"#{x['id']} {x['name']} ({x['created_at']})")
        elif args.cmd == "resume-delete":
            if db.execute("DELETE FROM resume_versions WHERE id=?", (args.id,)).rowcount == 0:
                raise ValueError("Resume version not found")
            print("Resume version deleted")
        elif args.cmd == "backup":
            destination = sqlite3.connect(args.file)
            try:
                db.backup(destination)
            finally:
                destination.close()
            if os.name == "posix":
                os.chmod(args.file, 0o600)
            print(f"Database backup written to {args.file}")
        elif args.cmd in ("interview-add", "followup-add", "note-add"):
            exists = db.execute("SELECT id FROM applications WHERE vacancy_id=?", (args.vacancy_id,)).fetchone()
            if exists is None:
                raise ValueError("Vacancy not found")
            if args.cmd == "interview-add":
                if datetime.fromisoformat(args.starts_at).utcoffset() is None:
                    raise ValueError("Use an ISO datetime with an explicit timezone offset")
                db.execute(
                    "INSERT INTO interview_events(application_id,starts_at,notes) VALUES(?,?,?)",
                    (exists[0], args.starts_at, args.notes),
                )
                print("Interview saved")
            elif args.cmd == "followup-add":
                if datetime.fromisoformat(args.due_at).utcoffset() is None:
                    raise ValueError("Use an ISO datetime with an explicit timezone offset")
                db.execute(
                    "INSERT INTO follow_up_tasks(application_id,due_at,note) VALUES(?,?,?)",
                    (exists[0], args.due_at, args.note),
                )
                print("Follow-up saved")
            else:
                db.execute(
                    "INSERT INTO notes(application_id,body,created_at) VALUES(?,?,?)", (exists[0], args.body, now())
                )
                print("Note saved")
        elif args.cmd == "interview-question-add":
            add_interview_question(db, args.interview_id, args.question)
            print("Interview question saved")
        elif args.cmd == "interview-note-add":
            add_interview_note(db, args.interview_id, args.body)
            print("Interview note saved")
        elif args.cmd == "followups":
            rows = db.execute(
                "SELECT f.due_at,f.note,v.title,c.name company FROM follow_up_tasks f JOIN applications a ON a.id=f.application_id JOIN vacancies v ON v.id=a.vacancy_id JOIN companies c ON c.id=v.company_id WHERE f.completed=0 ORDER BY f.due_at"
            ).fetchall()
            rows = sorted(rows, key=lambda row: datetime.fromisoformat(row["due_at"]).astimezone(UTC))
            for row in rows:
                state = (
                    "OVERDUE"
                    if datetime.fromisoformat(row["due_at"])
                    < datetime.now(datetime.fromisoformat(row["due_at"]).tzinfo)
                    else "DUE"
                )
                print(f"{state} {row['due_at']} — {row['company']} / {row['title']}: {row['note']}")
            if not rows:
                print("No pending follow-ups")
        elif args.cmd == "reminders":
            rows = due_reminders(db, record=args.record)
            for row in rows:
                print(f"REMINDER #{row['id']} {row['due_at']} — {row['company']} / {row['title']}: {row['note']}")
            if not rows:
                print("No new due reminders")
            db.commit()
        elif args.cmd == "followup-complete":
            complete_followup(db, args.id)
            print("Follow-up completed")
        elif args.cmd == "interviews":
            rows = db.execute(
                "SELECT i.id,i.starts_at,i.notes,v.title,c.name company FROM interview_events i JOIN applications a ON a.id=i.application_id JOIN vacancies v ON v.id=a.vacancy_id JOIN companies c ON c.id=v.company_id"
            ).fetchall()
            rows = sorted(rows, key=lambda row: datetime.fromisoformat(row["starts_at"]).astimezone(UTC))
            for row in rows:
                print(f"#{row['id']} {row['starts_at']} — {row['company']} / {row['title']}: {row['notes']}")
            if not rows:
                print("No interviews")
        elif args.cmd == "interview-questions":
            rows = db.execute(
                "SELECT q.interview_id,q.question,i.starts_at,v.title,c.name company FROM interview_questions q JOIN interview_events i ON i.id=q.interview_id JOIN applications a ON a.id=i.application_id JOIN vacancies v ON v.id=a.vacancy_id JOIN companies c ON c.id=v.company_id ORDER BY q.interview_id,q.id"
            ).fetchall()
            for row in rows:
                print(f"#{row['interview_id']} {row['starts_at']} — {row['company']} / {row['title']}: {row['question']}")
            if not rows:
                print("No interview preparation questions")
        elif args.cmd == "interview-notes":
            rows = db.execute(
                "SELECT n.interview_id,n.body,i.starts_at,v.title,c.name company FROM interview_post_notes n JOIN interview_events i ON i.id=n.interview_id JOIN applications a ON a.id=i.application_id JOIN vacancies v ON v.id=a.vacancy_id JOIN companies c ON c.id=v.company_id ORDER BY n.interview_id,n.id"
            ).fetchall()
            for row in rows:
                print(f"#{row['interview_id']} {row['starts_at']} — {row['company']} / {row['title']}: {row['body']}")
            if not rows:
                print("No post-interview notes")
        elif args.cmd == "dashboard":
            for status in STATUSES:
                items = list_vacancies(db, status=status)
                print(f"\n{status.upper()} ({len(items)})")
                for x in items:
                    print(f"  #{x['id']} {x['title']} — {x['company']}")
        elif args.cmd == "export":
            writer = csv.writer(sys.stdout)
            writer.writerow(["id", "title", "company", "status", "url", "source", "description"])
            for x in list_vacancies(db):
                writer.writerow(
                    [x["id"]] + [spreadsheet_safe_text(x[k]) for k in ("title", "company", "status", "url", "source", "description")]
                )
        elif args.cmd == "export-md":
            for status in STATUSES:
                items = list_vacancies(db, status=status)
                if not items:
                    continue
                print(f"## {status.title()}\n")
                for x in items:
                    link = f" — [{x['url']}]({x['url']})" if x["url"] else ""
                    print(f"- **{x['title']}** — {x['company']}{link}")
                    if x["description"]:
                        print(f"  \n  {x['description'][:800]}")
                print()
        elif args.cmd == "delete":
            cur = db.execute("DELETE FROM vacancies WHERE id=?", (args.id,))
            if not cur.rowcount:
                raise ValueError("Vacancy not found")
            db.execute(
                "DELETE FROM companies WHERE NOT EXISTS (SELECT 1 FROM vacancies WHERE vacancies.company_id=companies.id)"
            )
            print("Vacancy and related history deleted")
        elif args.cmd == "delete-all":
            db.execute("DELETE FROM vacancies")
            db.execute("DELETE FROM companies")
            print("All vacancy and application data deleted")
        db.commit()
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        db.rollback()
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
