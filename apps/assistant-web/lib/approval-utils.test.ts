import { describe, expect, it } from "vitest";

import { approvalPreviewText, approvalStatusCopy, isApprovalExpired } from "./approval-utils";

describe("approval display helpers", () => {
  it("uses an inert JSON preview for structured values", () => {
    expect(approvalPreviewText({ content: "<img src=x>" })).toContain("<img src=x>");
  });

  it("maps safe approval status copy and expiry", () => {
    expect(approvalStatusCopy({ durableStatus: "denied", uiStatus: "idle", expires_at: "2099-01-01T00:00:00Z" })).toContain("Denied");
    expect(isApprovalExpired({ durableStatus: "pending", expires_at: "2000-01-01T00:00:00Z" }, Date.parse("2026-01-01T00:00:00Z"))).toBe(true);
  });
});
