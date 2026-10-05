import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { randomString } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000/api/v1';

const auditPageTrend = new Trend('audit_page_duration');
const auditCursorTrend = new Trend('audit_cursor_duration');
const auditFilteredTrend = new Trend('audit_filtered_duration');
const errorRate = new Rate('errors');

export const options = {
  stages: [
    { target: 5, duration: '10s' },
    { target: 25, duration: '30s' },
    { target: 50, duration: '60s' },
    { target: 0, duration: '10s' },
  ],
  thresholds: {
    audit_page_duration: ['p(95)<200'],
    audit_cursor_duration: ['p(95)<200'],
    audit_filtered_duration: ['p(95)<200'],
    http_req_duration: ['p(95)<1000'],
    errors: ['rate<0.01'],
  },
};

export function setup() {
  // Login and seed audit events
  const loginRes = http.post(`${BASE_URL}/auth/login`, JSON.stringify({
    email: 'admin@modulo.test',
    password: 'test-password-123',
  }), {
    headers: { 'Content-Type': 'application/json' },
  });

  check(loginRes, {
    'setup login succeeded': (r) => r.status === 200,
  });

  if (loginRes.status !== 200) {
    throw new Error(`Setup login failed: ${loginRes.status}`);
  }

  const token = JSON.parse(loginRes.body).access_token;
  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
    },
  };

  // Seed 50 audit events by creating and updating pipelines
  for (let i = 0; i < 50; i++) {
    const pipelineRes = http.post(`${BASE_URL}/pipelines`, JSON.stringify({
      name: `seed-pipeline-${randomString(6)}`,
      description: `Seed event ${i} for audit testing`,
      visibility: 'org',
      max_concurrent_runs: 5,
    }), params);

    if (pipelineRes.status === 201) {
      const pipelineId = JSON.parse(pipelineRes.body).id;

      // Patch to generate an audit event for autonomy level change
      http.patch(`${BASE_URL}/pipelines/${pipelineId}`, JSON.stringify({
        default_autonomy_level: i % 2 === 0 ? 'fully_autonomous' : 'notify_on_complete',
      }), params);
    }
  }

  return { token };
}

export default function auditQuery(data) {
  const token = data.token;
  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
    },
  };

  group('Audit query pagination', function () {
    // PAGED QUERY
    group('Initial audit page', function () {
      const res = http.get(`${BASE_URL}/admin/audit?limit=50&page=1`, params);
      auditPageTrend.add(res.timings.duration);

      const passed = check(res, {
        'audit query status 200': (r) => r.status === 200,
        'audit returns items': (r) => {
          const body = JSON.parse(r.body);
          return Array.isArray(body.items) && body.items.length > 0;
        },
      });

      if (!passed) {
        errorRate.add(1);
        return;
      }
    });

    // CURSOR PAGINATION
    group('Cursor-based pagination', function () {
      // Get first page with cursor
      const firstRes = http.get(`${BASE_URL}/admin/audit?limit=10`, params);

      if (firstRes.status !== 200) {
        errorRate.add(1);
        return;
      }

      const firstBody = JSON.parse(firstRes.body);
      auditCursorTrend.add(firstRes.timings.duration);

      // Require BOTH a non-empty first page and a next cursor before following
      // it, so a follow-up page can be compared against a known first item.
      const firstPageOk = check(firstRes, {
        'first cursor page has items': () => Array.isArray(firstBody.items) && firstBody.items.length > 0,
        'first cursor page has next cursor': () =>
          typeof firstBody.next_cursor === 'string' && firstBody.next_cursor.length > 0,
      });

      // Follow the OPAQUE JSON cursor returned by the API. The audit endpoint
      // expects a JSON cursor string ({"c":<created_at>,"i":<id>}) — passing a
      // bare event id here would fail to decode and silently fall back to page
      // 1, measuring a first-page fetch instead of cursor traversal.
      const nextCursor = firstBody.next_cursor;
      if (firstPageOk && typeof nextCursor === 'string' && nextCursor.length > 0) {
        const cursorRes = http.get(`${BASE_URL}/admin/audit?limit=10&cursor=${encodeURIComponent(nextCursor)}`, params);
        auditCursorTrend.add(cursorRes.timings.duration);

        // Check the status BEFORE parsing: a non-200 body (e.g. a 5xx HTML
        // page) is not JSON, and an uncaught JSON.parse here would abort the
        // whole VU iteration instead of just failing a check.
        const cursorStatusOk = check(cursorRes, {
          'cursor page status 200': (r) => r.status === 200,
        });

        if (cursorStatusOk) {
          const cursorBody = JSON.parse(cursorRes.body);
          const firstItem = firstBody.items[0];
          check(cursorRes, {
            'cursor page returns items': () => Array.isArray(cursorBody.items),
            'cursor page does not repeat first page': () =>
              // Each seeded pipeline PATCH emits one pipeline.autonomy_level_changed
              // event, so with limit=10 there are guaranteed to be more pages and
              // the follow-up page must be non-empty AND disjoint from the first
              // page (the cursor boundary is strict older-than).
              cursorBody.items.length > 0 && cursorBody.items[0].id !== firstItem.id,
          });
        }
      }
    });

    // FILTERED QUERY
    group('Audit filtered query', function () {
      // Pipeline PATCHes that flip default_autonomy_level emit the
      // pipeline.autonomy_level_changed event — the ONLY pipeline-scoped event
      // type the seeding is guaranteed to generate (creation does not audit).
      // Asserting against a real emitted type instead of the aspirational
      // 'pipeline.updated' proves the filter actually filters.
      const res = http.get(`${BASE_URL}/admin/audit?event_type=pipeline.autonomy_level_changed&limit=20`, params);
      auditFilteredTrend.add(res.timings.duration);

      // The filter must actually filter: every returned event type must match.
      const passed = check(res, {
        'filtered audit status 200': (r) => r.status === 200,
        'filtered audit returns matching events': (r) => {
          const body = JSON.parse(r.body);
          return (
            Array.isArray(body.items) &&
            body.items.length > 0 &&
            body.items.every((item) => item.event_type === 'pipeline.autonomy_level_changed')
          );
        },
      });

      if (!passed) {
        errorRate.add(1);
      }
    });
  });

  sleep(1);
}

export function teardown(data) {
  if (!data || !data.token) return;

  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${data.token}`,
    },
  };

  const listRes = http.get(`${BASE_URL}/pipelines?page_size=100`, params);
  if (listRes.status === 200) {
    const pipelines = JSON.parse(listRes.body).items || [];
    for (const p of pipelines) {
      if (p.name && p.name.startsWith('seed-pipeline-')) {
        http.del(`${BASE_URL}/pipelines/${p.id}`, null, params);
      }
    }
  }
}
