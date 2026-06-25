import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { randomString } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';
import { uuidv4 } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000/api/v1';

const auditPageTrend = new Trend('audit_page_duration');
const auditCursorTrend = new Trend('audit_cursor_duration');
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
  const seededIds = [];
  for (let i = 0; i < 50; i++) {
    const pipelineRes = http.post(`${BASE_URL}/pipelines`, JSON.stringify({
      name: `seed-pipeline-${randomString(6)}`,
      description: `Seed event ${i} for audit testing`,
      visibility: 'org',
      max_concurrent_runs: 5,
    }), params);

    if (pipelineRes.status === 201) {
      const pipelineId = JSON.parse(pipelineRes.body).id;
      seededIds.push(pipelineId);

      // Patch to generate an audit event for autonomy level change
      http.patch(`${BASE_URL}/pipelines/${pipelineId}`, JSON.stringify({
        default_autonomy_level: i % 2 === 0 ? 'fully_autonomous' : 'notify_on_complete',
      }), params);
    }
  }

  return { token };
}

export default function (data) {
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

      check(firstRes, {
        'first cursor page has items': () => Array.isArray(firstBody.items) && firstBody.items.length > 0,
        'first cursor page has next cursor': () => firstBody.next_cursor !== undefined || firstBody.cursor !== undefined,
      });

      // Follow cursor if available
      const lastItem = firstBody.items[firstBody.items.length - 1];
      if (lastItem && lastItem.id) {
        const cursorRes = http.get(`${BASE_URL}/admin/audit?limit=10&cursor=${lastItem.id}`, params);
        auditCursorTrend.add(cursorRes.timings.duration);

        check(cursorRes, {
          'cursor page status 200': (r) => r.status === 200,
          'cursor page returns items': (r) => {
            const body = JSON.parse(r.body);
            return Array.isArray(body.items);
          },
        });
      }
    });

    // FILTERED QUERY
    group('Audit filtered query', function () {
      const res = http.get(`${BASE_URL}/admin/audit?event_type=pipeline.updated&limit=20`, params);

      check(res, {
        'filtered audit status 200': (r) => r.status === 200,
      });
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
