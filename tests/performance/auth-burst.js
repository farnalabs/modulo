import http from 'k6/http';
import { check, group } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000/api/v1';

const loginTrend = new Trend('login_duration');
const errorRate = new Rate('errors');
const fivexxRate = new Rate('http_5xx_errors');

export const options = {
  scenarios: {
    auth_burst: {
      executor: 'ramping-arrival-rate',
      startRate: 10,
      timeUnit: '1s',
      preAllocatedVUs: 50,
      maxVUs: 200,
      stages: [
        { target: 200, duration: '10s' },
        { target: 200, duration: '30s' },
        { target: 0, duration: '5s' },
      ],
    },
  },
  thresholds: {
    login_duration: ['p(95)<300'],
    http_req_duration: ['p(95)<1000'],
    errors: ['rate<0.01'],
    'http_5xx_errors': ['rate==0'],
  },
};

const testUsers = [
  { email: 'admin@modulo.test', password: 'test-password-123' },
  { email: 'user-one@modulo.test', password: 'test-password-123' },
  { email: 'user-two@modulo.test', password: 'test-password-123' },
  { email: 'viewer@modulo.test', password: 'test-password-123' },
  { email: 'editor@modulo.test', password: 'test-password-123' },
];

export default function () {
  group('Auth login burst', function () {
    // Rotate through test users to avoid token caching effects (non-security test jitter)
    const user = testUsers[Math.floor(Math.random() * testUsers.length)]; // NOSONAR: load-test user rotation, not security-sensitive

    const payload = JSON.stringify({
      email: user.email,
      password: user.password,
    });

    const res = http.post(`${BASE_URL}/auth/login`, payload, {
      headers: { 'Content-Type': 'application/json' },
      tags: { endpoint: 'auth-login' },
    });

    loginTrend.add(res.timings.duration);

    if (res.status >= 500) {
      fivexxRate.add(1);
    }

    check(res, {
      'login status 200': (r) => r.status === 200,
      'login returns access_token': (r) => JSON.parse(r.body).access_token !== undefined,
      'login returns refresh_token': (r) => JSON.parse(r.body).refresh_token !== undefined,
      'login token_type is bearer': (r) => JSON.parse(r.body).token_type === 'bearer',
      'no 5xx errors': (r) => r.status < 500,
    });
  });
}

export function handleSummary(data) {
  return {
    stdout: JSON.stringify({
      summary: 'Auth Burst Test Results',
      total_requests: data.metrics.http_reqs.values.count,
      login_p95_ms: data.metrics.login_duration ? data.metrics.login_duration.values['p(95)'].toFixed(2) : 'N/A',
      login_p99_ms: data.metrics.login_duration ? data.metrics.login_duration.values['p(99)'].toFixed(2) : 'N/A',
      error_rate: data.metrics.errors ? data.metrics.errors.values.rate.toFixed(4) : 'N/A',
      fivexx_rate: data.metrics['http_5xx_errors'] ? data.metrics['http_5xx_errors'].values.rate.toFixed(4) : 'N/A',
      passed: data.metrics.checks ? data.metrics.checks.values.passes : 0,
      failed: data.metrics.checks ? data.metrics.checks.values.fails : 0,
    }, null, 2),
  };
}
