const { execFile } = require("child_process");
const {
  safeErrorMessage,
  safeOutputSummary,
} = require("./safe-log");

const HERMES = process.env.HERMES_BIN || "/Users/balen/.hermes/hermes-agent/venv/bin/hermes";
const HERMES_ENV = {
  ...process.env,
  HERMES_PROFILE: process.env.HERMES_PROFILE || "trader",
  HERMES_ACCEPT_HOOKS: "1",
};

function createHermesCronJob(
  {
    name,
    prompt,
    skills = ["crypto-trader"],
    workdir = "/Users/balen/.openclaw/workspace-trader",
    repeat = "1",
    deliver = "telegram",
    createTimeoutMs = 30000,
  },
  cb
) {
  const args = ["cron", "create", new Date().toISOString(), prompt, "--name", name, "--deliver", deliver, "--repeat", repeat];
  for (const skill of skills) {
    args.push("--skill", skill);
  }
  if (workdir) {
    args.push("--workdir", workdir);
  }

  execFile(HERMES, args, { timeout: createTimeoutMs, env: HERMES_ENV }, (err, stdout, stderr) => {
    if (err) {
      return cb(err, stdout, stderr);
    }
    const match = stdout.match(/Created job:\s*(\S+)/);
    if (!match) {
      return cb(new Error("No job ID in hermes cron output"), stdout, stderr);
    }
    cb(null, { jobId: match[1], stdout, stderr });
  });
}

function runHermesCronJob(jobId, timeoutMs, cb) {
  execFile(HERMES, ["cron", "run", jobId, "--accept-hooks"], { timeout: timeoutMs, env: HERMES_ENV }, cb);
}

function triggerHermesCron({ logPrefix, name, prompt, timeoutSeconds = 120, skills, workdir }) {
  const runTimeoutMs = parsePositiveInteger(timeoutSeconds, 120) * 1000;
  createHermesCronJob({ name, prompt, skills, workdir, createTimeoutMs: runTimeoutMs }, (err, resultOrStdout, stderr) => {
    if (err) {
      console.log(`[${logPrefix}] hermes cron create error: ${safeErrorMessage(err)}`);
      if (stderr) {
        console.log(`[${logPrefix}] hermes stderr: ${safeOutputSummary(stderr)}`);
      }
      return;
    }
    const { jobId } = resultOrStdout;
    console.log(`[${logPrefix}] hermes cron created: ${jobId}`);
    runHermesCronJob(jobId, runTimeoutMs, (runErr, runOut, runStderr) => {
      if (runErr) {
        console.log(`[${logPrefix}] hermes cron run error: ${safeErrorMessage(runErr)}`);
        if (runStderr) {
          console.log(`[${logPrefix}] hermes run stderr: ${safeOutputSummary(runStderr)}`);
        }
        return;
      }
      console.log(`[${logPrefix}] hermes cron run queued: ${safeOutputSummary(runOut)}`);
    });
  });
}

function parsePositiveInteger(rawValue, fallback) {
  const value = Number.parseInt(String(rawValue || ""), 10);
  if (!Number.isFinite(value)) {
    return fallback;
  }
  if (value <= 0) {
    return fallback;
  }
  return value;
}

module.exports = {
  triggerHermesCron,
};
