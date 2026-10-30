import csv
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from job_hunter import (
    HHSource,
    add_interview_note,
    add_interview_question,
    add_vacancy,
    complete_followup,
    connect,
    due_reminders,
    list_vacancies,
    match_resume,
    set_status,
)


def test_status_history_and_search(tmp_path):
    db = connect(tmp_path / "jobs.db")
    vid = add_vacancy(db, "Data Engineer", "Acme", description="Required: Python, SQL")
    set_status(db, vid, "applied")
    assert list_vacancies(db, "acme")[0]["status"] == "applied"
    assert [r["new_status"] for r in db.execute("select new_status from status_history order by id")] == [
        "saved",
        "applied",
    ]
    set_status(db, vid, "applied")
    assert db.execute("select count(*) from status_history").fetchone()[0] == 2


def test_invalid_data_and_status(tmp_path):
    db = connect(tmp_path / "jobs.db")
    with pytest.raises(ValueError):
        add_vacancy(db, "", "Acme")
    with pytest.raises(ValueError):
        set_status(db, 404, "offer")


def test_vacancy_add_is_idempotent_for_url_and_manual_company_title(tmp_path):
    db = connect(tmp_path / "jobs.db")
    first = add_vacancy(db, "Engineer", "Acme")
    assert add_vacancy(db, " engineer ", "ACME") == first
    linked = add_vacancy(db, "Data Engineer", "Another Co", "https://jobs.example/role/1/")
    assert add_vacancy(db, "Different title", "Different Co", "https://jobs.example/role/1") == linked
    assert db.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 2


def test_status_transition_is_constrained_and_audited(tmp_path):
    db = connect(tmp_path / "jobs.db")
    vacancy = add_vacancy(db, "Engineer", "Acme")
    with pytest.raises(ValueError, match="Cannot move"):
        set_status(db, vacancy, "offer")
    assert db.execute("select count(*) from status_history").fetchone()[0] == 1


def test_followup_reminders_are_timezone_aware_daily_idempotent_and_completable(tmp_path):
    db = connect(tmp_path / "jobs.db")
    vacancy = add_vacancy(db, "Engineer", "Acme")
    app_id = db.execute("select id from applications where vacancy_id=?", (vacancy,)).fetchone()[0]
    task_id = db.execute(
        "insert into follow_up_tasks(application_id,due_at,note) values(?,?,?)",
        (app_id, "2026-03-29T01:30:00+01:00", "Review interview notes"),
    ).lastrowid
    now_at = datetime.fromisoformat("2026-03-29T03:15:00+02:00")

    assert due_reminders(db, now_at) [0]["id"] == task_id
    assert due_reminders(db, now_at, record=True)[0]["note"] == "Review interview notes"
    assert due_reminders(db, now_at, record=True) == []
    next_day = datetime.fromisoformat("2026-03-30T09:00:00+02:00")
    assert len(due_reminders(db, next_day, record=True)) == 1
    complete_followup(db, task_id)
    assert due_reminders(db, next_day, record=True) == []
    with pytest.raises(ValueError, match="Pending follow-up"):
        complete_followup(db, task_id)


def test_due_reminder_rejects_naive_clock(tmp_path):
    db = connect(tmp_path / "jobs.db")
    with pytest.raises(ValueError, match="timezone"):
        due_reminders(db, datetime(2026, 1, 1))


def test_interview_preparation_questions_and_post_notes_are_attached_to_event(tmp_path):
    db = connect(tmp_path / "jobs.db")
    vacancy = add_vacancy(db, "Engineer", "Acme")
    app_id = db.execute("select id from applications where vacancy_id=?", (vacancy,)).fetchone()[0]
    interview_id = db.execute(
        "insert into interview_events(application_id,starts_at,notes) values(?,?,?)",
        (app_id, "2026-11-01T09:00:00+03:00", "Screen call"),
    ).lastrowid

    add_interview_question(db, interview_id, "How do retries handle duplicate work?")
    add_interview_note(db, interview_id, "Discussed Python and SQL examples.")

    assert db.execute("select question from interview_questions where interview_id=?", (interview_id,)).fetchone()[0].startswith("How do")
    assert db.execute("select body from interview_post_notes where interview_id=?", (interview_id,)).fetchone()[0].startswith("Discussed")
    with pytest.raises(ValueError, match="Interview not found"):
        add_interview_note(db, 404, "Orphan note")


def test_old_job_database_opens_with_additive_reminder_schema(tmp_path):
    path = tmp_path / "older.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """CREATE TABLE companies(id INTEGER PRIMARY KEY,name TEXT NOT NULL UNIQUE);
        CREATE TABLE vacancies(id INTEGER PRIMARY KEY,company_id INTEGER,title TEXT,url TEXT,description TEXT,source TEXT,fetched_at TEXT,created_at TEXT);
        CREATE TABLE applications(id INTEGER PRIMARY KEY,vacancy_id INTEGER,status TEXT,updated_at TEXT);
        CREATE TABLE follow_up_tasks(id INTEGER PRIMARY KEY,application_id INTEGER,due_at TEXT,note TEXT,completed INTEGER DEFAULT 0);
        INSERT INTO companies VALUES(1,'Legacy Co');
        INSERT INTO vacancies VALUES(1,1,'Role','','','manual',NULL,'2026-01-01T00:00:00+00:00');
        INSERT INTO applications VALUES(1,1,'applied','2026-01-01T00:00:00+00:00');
        INSERT INTO follow_up_tasks VALUES(1,1,'2026-01-02T09:00:00+00:00','Legacy reminder',0);"""
    )
    legacy.commit()
    legacy.close()

    db = connect(path)
    reminders = due_reminders(db, datetime.fromisoformat("2026-01-02T10:00:00+00:00"), record=True)
    assert reminders[0]["note"] == "Legacy reminder"
    assert db.execute("select count(*) from follow_up_reminders").fetchone()[0] == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode check")
def test_database_file_permissions_are_owner_only(tmp_path):
    path = tmp_path / "private-jobs.sqlite3"
    db = connect(path)
    db.close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_interview_cli_sorts_by_instant_across_timezone_offsets(tmp_path):
    project = Path(__file__).resolve().parents[1]
    database = tmp_path / "jobs.sqlite3"

    def cli(*args):
        return subprocess.run(
            [sys.executable, "job_hunter.py", "--db", str(database), *args],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    cli("add", "Engineer", "Synthetic Co")
    cli("interview-add", "1", "2026-11-01T10:00:00-05:00", "--notes", "Later UTC")
    cli("interview-add", "1", "2026-11-01T14:00:00+03:00", "--notes", "Earlier UTC")
    agenda = cli("interviews")

    assert agenda.index("#2") < agenda.index("#1")


def test_csv_export_neutralizes_formula_like_untrusted_text(tmp_path):
    project = Path(__file__).resolve().parents[1]
    database = tmp_path / "jobs.sqlite3"
    output = subprocess.run(
        [sys.executable, "job_hunter.py", "--db", str(database), "add", "=HYPERLINK(\"https://example.invalid\")", "@SUM(1,1)"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Saved vacancy" in output.stdout
    exported = subprocess.run(
        [sys.executable, "job_hunter.py", "--db", str(database), "export"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    row = next(csv.DictReader(exported.stdout.splitlines()))
    assert row["title"].startswith("'=HYPERLINK")
    assert row["company"].startswith("'@SUM")


def test_match_is_evidence_based_and_bilingual():
    result = match_resume("Обязательно: Python и SQL. Желательно: Docker.", "Опыт: Python, SQL. Работал с PostgreSQL")
    assert "python" in result["matched"] and "sql" in result["matched"]
    assert "docker" in result["unmentioned"]
    assert result["score"] == 67
    assert "probability" in result["score_note"]


def test_empty_description_has_no_score():
    assert match_resume("", "Resume")["score"] is None


def test_hh_adapter_pagination_user_agent_timeout_and_cache():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            assert limit == 2_000_001
            return json.dumps(
                {
                    "items": [
                        {
                            "name": "Инженер", "employer": {"name": "Company"},
                            "alternate_url": "https://hh.ru/vacancy/1", "published_at": "2026-10-29T10:00:00+0300",
                        }
                    ]
                }
            ).encode()

    seen = []

    def fake(req, timeout):
        seen.append((req.full_url, req.get_header("User-agent"), timeout))
        return Response()

    source = HHSource(timeout=3)
    with patch("urllib.request.urlopen", fake):
        result = source.search("python", page=2)[0]
        assert result["source"] == "hh.ru" and result["published_at"] == "2026-10-29T10:00:00+0300"
        assert result["fetched_at"].endswith("+00:00")
        source.search("python", page=2)
    assert (
        len(seen) == 1 and "page=2" in seen[0][0] and seen[0][1].startswith("PersonalAutomationLab") and seen[0][2] == 3
    )


def test_hh_rate_limit_retry_after_then_success(monkeypatch):
    calls, sleeps = [], []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            return b'{"items": []}'

    def fake(req, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "2"}, io.BytesIO())
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr("time.sleep", sleeps.append)
    source = HHSource(timeout=4, backoff_seconds=0.1)
    assert source.search("python") == []
    assert calls == [4, 4] and sleeps == [2.0]


def test_hh_malformed_payload_fails_with_actionable_service_error(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            return b'{"items": [{"name":"Bad record", "employer":"unexpected"}]}'

    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: Response())
    with pytest.raises(TypeError, match="invalid employer"):
        HHSource().search("python")


def test_hh_cache_survives_source_and_database_recreation(tmp_path):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, _limit):
            return json.dumps({"items": [{"name": "Engineer", "employer": {"name": "Acme"},
                "alternate_url": "https://hh.ru/vacancy/1", "published_at": "2026-10-30T09:00:00+0300"}]}).encode()

    cache_path = tmp_path / "jobs.db"
    db = connect(cache_path)
    with patch("urllib.request.urlopen", return_value=Response()) as fetch:
        first = HHSource(cache_db=db).search("Python", page=1)
    db.commit()
    db.close()
    assert fetch.call_count == 1

    reopened = connect(cache_path)
    with patch("urllib.request.urlopen", side_effect=AssertionError("cache should avoid network")):
        cached = HHSource(cache_db=reopened).search("Python", page=1)
    assert cached == first
    reopened.close()


def test_match_can_select_a_saved_resume_version_in_cli(tmp_path):
    project = Path(__file__).resolve().parents[1]
    database = tmp_path / "jobs.db"
    resume = tmp_path / "resume.txt"
    resume.write_text("Опыт: Python и SQL в ETL-проектах.", encoding="utf-8")

    def cli(*args):
        return subprocess.run([sys.executable, "job_hunter.py", "--db", str(database), *args],
            cwd=project, check=True, capture_output=True, text=True).stdout

    cli("add", "Data Engineer", "Acme", "--description", "Обязательно: Python и SQL")
    cli("resume-add", "2026 CV", str(resume))
    result = json.loads(cli("match", "1", "--resume-version", "1"))
    assert set(result["matched"]) == {"python", "sql"}
