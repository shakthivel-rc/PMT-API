"""The report engine (docs/design/08-reports.md §5.3).

* `params` resolves a request's scope and dates; `scope` turns them into tenant-filtered
  queries; `history` replays `issue_events` to answer "what was true on day D".
* One module per report family under `types_*`, each a function from a `ReportContext` to
  a `ReportResult` dict, registered in `registry.REPORTS` with its catalogue entry.
* `cache` memoises results for a minute; `exporters` turn a result into CSV, XLSX or PDF;
  `jobs` and `scheduler` run the large and the recurring ones in `workers/report_worker.py`.

Nothing here commits. Controllers own the transaction, as everywhere (CLAUDE.md §4).
"""
