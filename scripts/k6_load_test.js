import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';
import { AWSConfig, SQSClient } from 'https://jslib.k6.io/aws/0.14.0/sqs.js';

const profileName = __ENV.K6_PROFILE || 'steady';
const type = (__ENV.type || __ENV.K6_TYPE || 'request').toLowerCase();
const profiles = JSON.parse(open('./k6_profiles.json'));
const profile = profiles[profileName];

if (!profile) {
  throw new Error(`Unknown K6_PROFILE '${profileName}'. Valid options: ${Object.keys(profiles).join(', ')}`);
}

if (type !== 'request' && type !== 'event') {
  throw new Error(`Unknown type '${type}'. Valid options: request, event`);
}

if (type === 'request' && !profile.endpoint) {
  throw new Error(`Profile '${profileName}' is missing endpoint`);
}

if (type === 'event' && !profile.queue_url) {
  throw new Error(`Profile '${profileName}' is missing queue_url`);
}

function loadAwsCredentials() {
  let fromFile = {};
  try {
    fromFile = JSON.parse(open('./.k6-aws-creds.json'));
  } catch (err) {
    fromFile = {};
  }

  const accessKeyId = __ENV.AWS_ACCESS_KEY_ID || fromFile.AccessKeyId || fromFile.accessKeyId;
  const secretAccessKey =
    __ENV.AWS_SECRET_ACCESS_KEY || fromFile.SecretAccessKey || fromFile.secretAccessKey;
  const sessionToken =
    __ENV.AWS_SESSION_TOKEN || fromFile.SessionToken || fromFile.sessionToken || '';
  const region =
    __ENV.AWS_REGION || fromFile.Region || fromFile.region || 'sa-east-1';

  return { accessKeyId, secretAccessKey, sessionToken, region };
}

let sqs = null;
if (type === 'event') {
  const creds = loadAwsCredentials();
  if (!creds.accessKeyId || !creds.secretAccessKey) {
    throw new Error(
      'AWS credentials required for type=event (env AWS_* or .k6-aws-creds.json)',
    );
  }

  const awsConfigOptions = {
    region: creds.region,
    accessKeyId: creds.accessKeyId,
    secretAccessKey: creds.secretAccessKey,
  };
  if (creds.sessionToken) {
    awsConfigOptions.sessionToken = creds.sessionToken;
  }
  sqs = new SQSClient(new AWSConfig(awsConfigOptions));
}

const payload = JSON.parse(open('./mock_data.json'));
const messageBody = JSON.stringify(payload);

function formatOffsetIso(date, offsetHours) {
  const shifted = new Date(date.getTime() + offsetHours * 60 * 60 * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const sign = offsetHours >= 0 ? '+' : '-';
  const abs = Math.abs(offsetHours);
  return (
    `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}` +
    `T${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}:${pad(shifted.getUTCSeconds())}` +
    `${sign}${pad(abs)}:00`
  );
}

const rampUp = profile.rampUp || '30s';

export function setup() {
  return { startedAtMs: Date.now() };
}

export const options = {
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(50)', 'p(95)', 'p(99)'],
  scenarios: {
    load_profile: {
      executor: 'ramping-arrival-rate',
      startRate: 0,
      timeUnit: profile.timeUnit,
      preAllocatedVUs: profile.preAllocatedVUs,
      maxVUs: profile.maxVUs,
      gracefulStop: profile.gracefulStop,
      stages: [
        { target: profile.rate, duration: rampUp },
        { target: profile.rate, duration: profile.duration },
      ],
      exec: type === 'request' ? 'requestScenario' : 'eventScenario',
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<15000'],
    http_req_failed: ['rate<0.05'],
    checks: ['rate>0.95'],
  },
  tags: {
    kind: type,
    profile: profileName,
  },
};

export function requestScenario() {
  const res = http.post(profile.endpoint, messageBody, {
    headers: {
      'Content-Type': 'application/json',
      'User-Agent': 'k6-loadtest/1.0',
    },
    timeout: profile.timeout,
  });

  check(res, {
    'status is 2xx': (r) => r.status >= 200 && r.status < 300,
    'status is not 5xx': (r) => r.status < 500,
    'under 15s': (r) => r.timings.duration < 15000,
  });
}

export async function eventScenario() {
  let messageId = null;
  let error = null;
  const started = Date.now();

  try {
    const response = await sqs.sendMessage(profile.queue_url, messageBody, {
      messageAttributes: {
        Source: { type: 'String', value: 'k6-loadtest' },
        MessageNum: {
          type: 'String',
          value: String(exec.scenario.iterationInTest),
        },
        Timestamp: { type: 'String', value: new Date().toISOString() },
      },
    });
    messageId = response.id || null;
  } catch (err) {
    error = err;
  }

  const elapsedMs = Date.now() - started;
  check(null, {
    'sqs send ok': () => error === null && messageId !== null,
    'under 15s': () => elapsedMs < 15000,
  });
}

export function handleSummary(data) {
  const duration = data.metrics.http_req_duration?.values || {};
  const total = data.metrics.http_reqs?.values?.count || 0;
  const throughput = data.metrics.http_reqs?.values?.rate || 0;
  const startedAtMs = data.setup_data?.startedAtMs;
  const startedAt = startedAtMs ? new Date(startedAtMs) : new Date();
  const endedAt = new Date();
  const checks = data.metrics.checks?.values || {};
  const httpFailed = data.metrics.http_req_failed?.values || {};
  const failedRate =
    type === 'event'
      ? 1 - (checks.rate ?? 1)
      : httpFailed.rate || 0;
  const failedCount =
    type === 'event'
      ? Math.max(0, (checks.fails ?? 0))
      : httpFailed.passes || 0;
  const summary = {
    kind: type,
    profile: profileName,
    target_rps: profile.rate,
    ramp_up: rampUp,
    duration: profile.duration,
    start_time: formatOffsetIso(startedAt, -3),
    end_time: formatOffsetIso(endedAt, -3),
    avg_ms: duration.avg || 0,
    p50_ms: duration['p(50)'] ?? duration.med ?? 0,
    p95_ms: duration['p(95)'] || 0,
    p99_ms: duration['p(99)'] || 0,
    failed_rate: failedRate,
    failed_count: failedCount,
  };

  if (type === 'request') {
    summary.total_requests = total;
    summary.throughput_req_s = throughput;
  } else {
    summary.total_events = total;
    summary.throughput_events_s = throughput;
  }

  const body = JSON.stringify(summary, null, 2) + '\n';
  const timestamp = new Date().toISOString().replace(/:/g, '-');
  const outFile = `results/k6-${type}-${profileName}-${timestamp}.json`;

  return {
    stdout: body,
    [outFile]: body,
  };
}
