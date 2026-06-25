import http from 'k6/http';
import { check, sleep, group } from 'k6';
import { Rate, Trend } from 'k6/metrics';
import { randomString } from 'https://jslib.k6.io/k6-utils/1.2.0/index.js';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000/api/v1';

const runCreateTrend = new Trend('run_create_duration');
const runPollTrend = new Trend('run_poll_duration');
const errorRate = new Rate('errors');

export const options = {
  stages: [
    { target: 5, duration: '10s' },
    { target: 25, duration: '30s' },
    { target: 50, duration: '60s' },
    { target: 0, duration: '10s' },
  ],
  thresholds: {
    run_create_duration: ['p(95)<1000'],
    run_poll_duration: ['p(95)<500'],
    http_req_duration: ['p(95)<2000'],
    errors: ['rate<0.01'],
  },
};

export function setup() {
  // Login and create a pipeline to trigger runs against
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

  // Create the pipeline that runs will target
  const pipelineRes = http.post(`${BASE_URL}/pipelines`, JSON.stringify({
    name: `perf-target-${randomString(8)}`,
    description: 'Performance test target pipeline',
    visibility: 'org',
    max_concurrent_runs: 50,
    lock_wait_timeout_seconds: 300,
    node_timeout_seconds: 60,
    run_context_defaults: {},
    default_autonomy_level: 'fully_autonomous',
  }), params);

  check(pipelineRes, {
    'setup pipeline created': (r) => r.status === 201,
  });

  if (pipelineRes.status !== 201) {
    throw new Error(`Setup pipeline creation failed: ${pipelineRes.status}`);
  }

  const pipelineId = JSON.parse(pipelineRes.body).id;

  return { token, pipelineId };
}

export default function (data) {
  const token = data.token;
  const pipelineId = data.pipelineId;

  const params = {
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`,
    },
  };

  group('Run execution', function () {
    // TRIGGER RUN
    group('Trigger run', function () {
      const payload = JSON.stringify({
        pipeline_id: pipelineId,
        input_payload: {
          message: `Performance test run at ${Date.now()}`,
          source: 'k6-load-test',
        },
      });

      const res = http.post(`${BASE_URL}/runs`, payload, params);
      runCreateTrend.add(res.timings.duration);

      const passed = check(res, {
        'trigger run status 202': (r) => r.status === 202,
        'trigger run returns run_id': (r) => JSON.parse(r.body).run_id !== undefined,
        'trigger run status is pending': (r) => JSON.parse(r.body).status === 'pending',
      });

      if (!passed) {
        errorRate.add(1);
        return;
      }

      const runId = JSON.parse(res.body).run_id;

      // POLL UNTIL COMPLETE
      group('Poll run status', function () {
        let attempts = 0;
        const maxAttempts = 30;
        let terminal = false;

        while (!terminal && attempts < maxAttempts) {
          sleep(1);

          const pollRes = http.get(`${BASE_URL}/runs/${runId}`, params);
          runPollTrend.add(pollRes.timings.duration);

          const pollCheck = check(pollRes, {
            'poll run status 200': (r) => r.status === 200,
          });

          if (!pollCheck) {
            errorRate.add(1);
            break;
          }

          const status = JSON.parse(pollRes.body).status;
          const terminalStatuses = ['complete', 'failed', 'cancelled'];

          if (terminalStatuses.includes(status)) {
            terminal = true;

            check(pollRes, {
              'run eventually completed': () => status === 'complete',
            });
          }

          attempts++;
        }

        if (!terminal) {
          errorRate.add(1);
        }
      });
    });
  });

  sleep(1);
}
