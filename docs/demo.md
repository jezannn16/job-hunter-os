# Demo: Job Hunter OS

Run these commands from this folder; use a throwaway database to avoid touching personal data:

```sh
python job_hunter.py --db /tmp/job-hunter-demo.sqlite3 add 'Data Engineer' 'Northwind' --url https://example.org/jobs/42 --description 'Required: Python, SQL. Preferred: Docker.'
python job_hunter.py --db /tmp/job-hunter-demo.sqlite3 dashboard
python job_hunter.py --db /tmp/job-hunter-demo.sqlite3 status 1 applied
python job_hunter.py --db /tmp/job-hunter-demo.sqlite3 dashboard
printf 'Python, SQL in ETL projects.' > /tmp/resume-demo.txt
python job_hunter.py --db /tmp/job-hunter-demo.sqlite3 match 1 /tmp/resume-demo.txt
python job_hunter.py --db /tmp/job-hunter-demo.sqlite3 export
python job_hunter.py --db /tmp/job-hunter-demo.sqlite3 backup /tmp/job-hunter-backup.sqlite3
python job_hunter.py --db /tmp/job-hunter-demo.sqlite3 reminder-scheduler
```

Expected behavior: vacancy starts in `saved`, the transition appears in the dashboard, matching returns resume snippets for Python and SQL and leaves Docker unmentioned, CSV export includes the source and status, and the reminder scheduler reports that notifications are disabled without requiring credentials or making a network request. HH.ru search can be tried separately with `hh-search` and requires network access.
