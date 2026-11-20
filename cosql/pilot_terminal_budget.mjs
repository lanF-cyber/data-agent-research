import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

export const inject = ["tools"];

export function apply(ctx, config) {
  let currentTurn;
  let dispatched = 0;
  let submissions = 0;
  const workspace = resolve(config.workspace).toLowerCase();
  ctx.on("tools/pre-execute", async (execution, next) => {
    const isSubmission = config.submitTool && execution.name === config.submitTool;
    if (execution.name !== "pwsh" && !isSubmission) {
      return { kind: "deny", reason: "This pilot provides the local PowerShell tool." };
    }
    const target = execution.arguments?.workdir;
    if (target && resolve(config.workspace, target).toLowerCase() !== workspace) {
      return { kind: "deny", reason: "Start commands in the current dialogue directory." };
    }
    const turn = readFileSync(config.turnFile, "utf8").trim();
    if (!turn) return { kind: "deny", reason: "No active question." };
    if (turn !== currentTurn) {
      currentTurn = turn;
      dispatched = 0;
      submissions = 0;
    }
    if (isSubmission) {
      if (submissions >= config.submitLimit) {
        return { kind: "deny", reason: "Answer submission budget reached." };
      }
      submissions += 1;
      return next();
    }
    if (dispatched >= config.limit) {
      return { kind: "deny", reason: "Terminal budget reached. Answer using evidence already obtained." };
    }
    dispatched += 1;
    return next();
  });
  writeFileSync(config.readyFile, "ready\n");
}
