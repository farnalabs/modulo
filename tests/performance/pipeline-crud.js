import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { randomString } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000/api/v1';

const pipelineCreateTrend = new Trend('pipeline_create_duration');
const pipelineListTrend = new Trend('pipeline_list_duration');
const pipelineGetTrend = new Trend('pipeline_get_duration');
const pipelineUpdateTrend = new Trend('pipeline_update_duration');
const pipelineDeleteTrend = new Trend('pipeline_delete_duration');
const errorRate = new Rate('errors');

export const options = {
  stages: [
    { target: 10, duration: '10s' },
    { target: 50, duration: '30s' },
    { target: 100, duration: '60s' },
    { target: 0, duration: '10s' },
  ],
  thresholds: {
    pipeline_create_duration: ['p(95)<500'],
    pipeline_list_duration: ['p(95)<500'],
    pipeline_get_duration: ['p(95)<500'],
    pipeline_update_duration: ['p(95)<500'],
    pipeline_delete_duration: ['p(95)<500'],
    errors: ['rate<0.01'],
    http_req_duration: ['p(95)<2000'],
  },
};

let authToken = null;
let createdPipelineIds = [];

export function setup() {
  const loginRes = http.post(`${BASE_URL}/auth/login`, JSON.stringify({
    email: 'admin@modulo.test',
    password: 'test-password-123',
  }), {
    headers: { 'Content-Type': 'application/json' },
  });

  check(loginRes, {
    'login succeeded': (r) => r.status === 200,
    'has access_token': (r) => JSON.parse(r.body).access_token !== undefined,
  });

  if (loginRes.status !== 200) {
    throw new Error(`Setup login failed: ${loginRes.status} ${loginRes.body}`);
  }

  return { token: JSON.parse(loginRes.body).access_token };
}

export default function (data) {
  const token = data.token;
  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
    },
  };

  group('Pipeline CRUD', function () {
    let pipelineId;

    // CREATE
    group('Create pipeline', function () {
      const name = `perf-pipeline-${randomString(8)}`;
      const payload = JSON.stringify({
        name: name,
        description: 'Performance test pipeline',
        visibility: 'org',
        max_concurrent_runs: 5,
        lock_wait_timeout_seconds: 300,
        node_timeout_seconds: 300,
        run_context_defaults: {},
        default_autonomy_level: 'manual_approval',
      });

      const res = http.post(`${BASE_URL}/pipelines`, payload, params);
      pipelineCreateTrend.add(res.timings.duration);

      const passed = check(res, {
        'create pipeline status 201': (r) => r.status === 201,
        'create pipeline has id': (r) => JSON.parse(r.body).id !== undefined,
      });

      if (!passed) {
        errorRate.add(1);
        return;
      }

      pipelineId = JSON.parse(res.body).id;
      createdPipelineIds.push(pipelineId);
    });

    if (!pipelineId) return;

    // LIST
    group('List pipelines', function () {
      const res = http.get(`${BASE_URL}/pipelines?page=1&page_size=20`, params);
      pipelineListTrend.add(res.timings.duration);

      const passed = check(res, {
        'list pipelines status 200': (r) => r.status === 200,
        'list returns items array': (r) => Array.isArray(JSON.parse(r.body).items),
      });

      if (!passed) errorRate.add(1);
    });

    // GET
    group('Get pipeline', function () {
      const res = http.get(`${BASE_URL}/pipelines/${pipelineId}`, params);
      pipelineGetTrend.add(res.timings.duration);

      const passed = check(res, {
        'get pipeline status 200': (r) => r.status === 200,
        'get pipeline returns correct id': (r) => JSON.parse(r.body).id === pipelineId,
      });

      if (!passed) errorRate.add(1);
    });

    // UPDATE
    group('Update pipeline', function () {
      const updatedDesc = `Updated at ${Date.now()}`;
      const payload = JSON.stringify({
        description: updatedDesc,
        default_autonomy_level: 'notify_on_complete',
      });

      const res = http.patch(`${BASE_URL}/pipelines/${pipelineId}`, payload, params);
      pipelineUpdateTrend.add(res.timings.duration);

      const passed = check(res, {
        'update pipeline status 200': (r) => r.status === 200,
        'update returns updated description': (r) =>
          JSON.parse(r.body).description === updatedDesc,
      });

      if (!passed) errorRate.add(1);
    });

    // DELETE
    group('Delete pipeline', function () {
      const res = http.del(`${BASE_URL}/pipelines/${pipelineId}`, null, params);
      pipelineDeleteTrend.add(res.timings.duration);

      const passed = check(res, {
        'delete pipeline status 204': (r) => r.status === 204,
      });

      if (!passed) errorRate.add(1);
    });

    sleep(1);
  });
}

export function teardown(data) {
  if (!data || !data.token) return;

  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${data.token}`,
    },
  };

  // Clean up any pipelines that weren't deleted during the test
  for (const id of createdPipelineIds) {
    http.del(`${BASE_URL}/pipelines/${id}`, null, params);
  }
}
