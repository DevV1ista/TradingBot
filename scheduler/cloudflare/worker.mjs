// Independent free scheduler. Never stores the GitHub token in the repository.
const SCHEDULES = {
  "39 4,5 * * MON-FRI": { segment: 1, nyHour: 0, minute: 39 },
  "29 9,10 * * MON-FRI": { segment: 2, nyHour: 5, minute: 29 },
  "49 14,15 * * MON-FRI": { segment: 3, nyHour: 10, minute: 49 },
};

export function resolveSchedule(cron, scheduledTime) {
  const config = SCHEDULES[cron];
  if (!config) throw new Error("Unexpected cron: " + cron);

  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
      timeZoneName: "short",
    }).formatToParts(new Date(scheduledTime)).map((p) => [p.type, p.value]),
  );

  // Each trigger fires twice (summer/winter). Only the active NY slot dispatches.
  if (Number(parts.hour) !== config.nyHour || Number(parts.minute) !== config.minute) {
    return null;
  }
  if (parts.timeZoneName !== "EST" && parts.timeZoneName !== "EDT") {
    throw new Error("Unexpected New York time zone: " + parts.timeZoneName);
  }
  return {
    segment: String(config.segment),
    slot: parts.timeZoneName === "EDT" ? "edt" : "est",
  };
}

export default {
  async scheduled(controller, env) {
    const selected = resolveSchedule(controller.cron, controller.scheduledTime);
    if (!selected) {
      console.log("Inactive summer/winter slot: " + controller.cron);
      return;
    }

    // Deploy in safe mode first. No GitHub calls until explicitly enabled.
    if (env.ENABLED !== "true") {
      console.log("DRY RUN - would dispatch " + JSON.stringify(selected));
      return;
    }
    if (!env.GITHUB_TOKEN) {
      throw new Error("Missing GITHUB_TOKEN Worker secret");
    }

    const response = await fetch(
      "https://api.github.com/repos/DevV1ista/TradingBot/actions/workflows/signals.yml/dispatches",
      {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": "Bearer " + env.GITHUB_TOKEN,
          "Content-Type": "application/json",
          "User-Agent": "tradingbot-free-scheduler",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({
          ref: "main",
          inputs: { job: "segment", ...selected },
        }),
      },
    );
    if (response.status !== 204 && response.status !== 200) {
      const body = (await response.text()).slice(0, 300);
      throw new Error("GitHub dispatch failed: HTTP " + response.status + " " + body);
    }
    console.log("GitHub dispatch accepted: " + JSON.stringify(selected));
  },
  async fetch() {
    return new Response("Scheduled access only", { status: 404 });
  },
};
