// k6 load test for synchronous reports (docs/design/08-reports.md §9).
//
// Target: p95 under 800 ms for every report type on a workspace with about 10k issues and
// 50k time entries. Run it against a seeded stack, never production:
//
//   k6 run -e BASE=http://localhost:8000/api/v1 -e TOKEN=<access token> \
//          -e WS=<workspace id> -e SPACE=<space id> loadtest/reports.js
//
// The report cache keys on the parameters, so each iteration picks a random end date: that
// forces most requests to recompute, which is the cost being measured. (A cached answer is a
// single primary-key read and would flatter the number.)
import http from 'k6/http';
import { check } from 'k6';

const BASE = __ENV.BASE || 'http://localhost:8000/api/v1';
const WS = __ENV.WS;
const SPACE = __ENV.SPACE;
const HEADERS = { Authorization: `Bearer ${__ENV.TOKEN}` };

const TYPES = [
  'timesheet', 'time_by_group', 'estimate_vs_actual', 'weekly', 'project_overview',
  'created_vs_resolved', 'workload', 'epic_progress', 'cfd', 'cycle_time',
];

export const options = {
  scenarios: {
    reports: { executor: 'constant-vus', vus: 20, duration: '2m' },
  },
  thresholds: {
    'http_req_duration{kind:report}': ['p(95)<800'],
    http_req_failed: ['rate<0.01'],
  },
};

function day(offset) {
  return new Date(Date.UTC(2026, 8, 1 + offset)).toISOString().slice(0, 10);
}

export default function () {
  const type = TYPES[Math.floor(Math.random() * TYPES.length)];
  // Up to 80 days: inside the 92-day limit of the time reports.
  const to = day(20 + Math.floor(Math.random() * 60));
  const url = `${BASE}/workspaces/${WS}/reports/${type}?scope=space:${SPACE}` +
    `&date_from=${day(0)}&date_to=${to}`;
  const res = http.get(url, { headers: HEADERS, tags: { kind: 'report', type } });
  check(res, { 'status is 200': (r) => r.status === 200 });
}
