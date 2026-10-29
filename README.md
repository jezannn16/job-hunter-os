# Job Hunter OS

Local-first job search CRM. Current MVP is a Python CLI backed by SQLite; it stores manual/imported vacancies, application status history, resume versions and interview/follow-up notes. HH.ru search is read-only and only runs when explicitly requested.

## Implemented

- Add vacancies manually or import description text from a UTF-8 file.
- Search/filter saved vacancies, show a status-grouped dashboard, keep application status transitions in an audit history, and export CSV.
- Compare vacancy description with a resume file using a small RU/EN skill vocabulary. Output includes exact resume snippets; unmentioned means unknown. Score is a heuristic overlap, never an offer probability.
- Read-only HH.ru public vacancy search with page selection, an identifying User-Agent and an 8-second timeout. Manual storage works without credentials.
- Save resume versions, interview events, follow-up tasks, and application notes in SQLite.
- Delete a vacancy and its history or all vacancy/application records.

## Quick start

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install pytest ruff
python job_hunter.py --db jobs.sqlite3 add 'Data Engineer' 'Example Co' --url https://example.org/job --description 'Required: Python, SQL'
python job_hunter.py --db jobs.sqlite3 list
python job_hunter.py --db jobs.sqlite3 status 1 applied
python job_hunter.py --db jobs.sqlite3 dashboard
python job_hunter.py --db jobs.sqlite3 export > jobs.csv
python job_hunter.py --db jobs.sqlite3 resume-add 'CV Oct 2026' resume.txt
python job_hunter.py --db jobs.sqlite3 interview-add 1 '2026-11-04T14:00:00+03:00' --notes 'Technical round'
python job_hunter.py --db jobs.sqlite3 followup-add 1 '2026-11-05T10:00:00+03:00' 'Ask for next steps'
python job_hunter.py --db jobs.sqlite3 followups
python job_hunter.py --db jobs.sqlite3 backup jobs-backup.sqlite3
python job_hunter.py --db jobs.sqlite3 export-md > applications.md
pytest -q
```

Search HH.ru: `python job_hunter.py hh-search 'аналитик данных' --page 0 --save`. The API is documented as `GET /vacancies`; anonymous search is available, while most user-specific methods require OAuth. See [HH.ru API vacancy documentation](https://github.com/hhru/api/blob/master/docs/vacancies.md) and [authorization documentation](https://github.com/hhru/api/blob/master/docs/authorization.md).

## Limitations / planned

No website scraping, auto-apply, message sending, LLM, calendar UI, scheduled reminder delivery, XLSX import or sophisticated Russian morphology. Interview and follow-up records are stored and visible in CLI lists, but no scheduler notifies the user. A match only recognizes a deliberately short synonym set. Database is single-user local SQLite without encryption. Imported vacancy text and resume versions are stored in full; avoid sensitive personal data. HH search accepts one page number per call and keeps only a short in-process cache; the CLI exits after each command so repeated CLI calls do not share that cache. Request failures leave existing records untouched. No API credentials are needed for the implemented public search.

See [demo steps](docs/demo.md) for a synthetic end-to-end walkthrough.
