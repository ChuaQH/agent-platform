import { describe, expect, it } from "vitest";

import { mergeRunSummaries, parseRunSummaries, previewText, readableReason, runStatusLabel, type ThreadRunSummary } from "./governed-run-utils";

const valid: ThreadRunSummary = {
  run_id: "run-1", strategy: "react", status: "completed", created_at: "2026-09-26T00:00:00Z", updated_at: "2026-09-26T00:01:00Z",
  completed_at: "2026-09-26T00:01:00Z", terminal_reason: null, action_count: 2, executed_count: 1, succeeded_count: 1,
  rejected_count: 1, waiting_for_approval_count: 0, uncertain_count: 0, has_pending_approval: false,
};

describe("governed-run display helpers", () => {
  it("renders safe reason and preview data without HTML interpretation", () => {
    expect(readableReason("approval_denied")).toContain("not dispatched");
    expect(readableReason("custom_code")).toBe("Gateway reason: custom code.");
    expect(previewText({ value: "<script>inert</script>" })).toContain("<script>inert</script>");
    expect(runStatusLabel("waiting_for_approval")).toBe("Waiting for approval");
  });

  it("whitelists valid run summaries and drops malformed or extra records", () => {
    const summaries = parseRunSummaries([
      { ...valid, policy: { secret: "never-rendered" }, arguments: { path: "nope" } },
      { ...valid, run_id: "bad-status", status: "unknown" },
      { run_id: "missing-fields" },
    ]);
    expect(summaries).toEqual([valid]);
    expect(JSON.stringify(summaries)).not.toContain("secret");
    expect(JSON.stringify(summaries)).not.toContain("arguments");
  });

  it("merges cursor pages without duplicate runs and keeps newest-first ordering", () => {
    const older = { ...valid, run_id: "run-0", updated_at: "2026-09-26T00:00:00Z" };
    const newer = { ...valid, run_id: "run-2", updated_at: "2026-09-26T00:02:00Z" };
    expect(mergeRunSummaries([older, valid], [newer, valid]).map((item) => item.run_id)).toEqual(["run-2", "run-1", "run-0"]);
  });
});
