import assert from "node:assert/strict";
import scheduler, { resolveSchedule } from "./worker.mjs";

const schedules = [
  ["39 4,5 * * MON-FRI", "2026-11-20T05:39:00.000Z", "1", "est"],
  ["39 4,5 * * MON-FRI", "2026-07-20T04:39:00.000Z", "1", "edt"],
  ["29 9,10 * * MON-FRI", "2026-11-20T10:29:00.000Z", "2", "est"],
  ["29 9,10 * * MON-FRI", "2026-07-20T09:29:00.000Z", "2", "edt"],
  ["49 14,15 * * MON-FRI", "2026-11-20T15:49:00.000Z", "3", "est"],
  ["49 14,15 * * MON-FRI", "2026-07-20T14:49:00.000Z", "3", "edt"],
];
for (const [cron, when, segment, slot] of schedules) {
  assert.deepEqual(resolveSchedule(cron, Date.parse(when)), { segment, slot });
}
assert.equal(resolveSchedule(schedules[0][0], Date.parse("2026-11-20T04:39:00Z")), null);
assert.equal(resolveSchedule(schedules[0][0], Date.parse("2026-07-20T05:39:00Z")), null);

const savedFetch = globalThis.fetch;
let calls = [];
try {
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return { status: 204 };
  };
  const c = { cron: schedules[0][0], scheduledTime: Date.parse(schedules[0][1]) };
  await scheduler.scheduled(c, { ENABLED: "false" });
  assert.equal(calls.length, 0, "Dry-run must not dispatch");
  await scheduler.scheduled(c, { ENABLED: "true", GITHUB_TOKEN: "fake-test-token" });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].url.includes("/actions/workflows/signals.yml/dispatches"));
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    ref: "main",
    inputs: { job: "segment", segment: "1", slot: "est" },
  });
  assert.equal(calls[0].options.headers.Authorization, "Bearer fake-test-token");
} finally {
  globalThis.fetch = savedFetch;
}
console.log("All scheduler tests passed (mocked GitHub request, no real dispatch).");
