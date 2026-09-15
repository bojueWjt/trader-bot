import { afterEach, describe, expect, it, vi } from "vitest";
import {
  closePosition, fetchDashboardOverview, fetchOrderCenter, moveStopLoss,
  partialClosePosition, POSITION_OPERATION_STORAGE_PREFIX, queryPositionOperation
} from "../utils/api";

const position = { accountId: "account-c", id: "BTCUSDT-PERP.BINANCE-SHORT", pair: "BTC/USDT", side: "short" };
const operationId = "11111111-1111-4111-8111-111111111111";

function response(payload: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => payload } as Response;
}

afterEach(() => {
  sessionStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("manual position operator API", () => {
  it.each(["fetch", "body"])("times out a stalled POST %s and retries the same saved request", async (stage) => {
    vi.useFakeTimers();
    const stalled = stage === "fetch"
      ? new Promise<Response>(() => {})
      : Promise.resolve({ ...response({}), json: () => new Promise(() => {}) });
    const fetchMock = vi.fn()
      .mockReturnValueOnce(stalled)
      .mockResolvedValueOnce(response({ intent_id: operationId, status: "accepted" }));
    vi.stubGlobal("fetch", fetchMock);
    const pending = closePosition(position, "initial reason");
    const firstRequest = fetchMock.mock.calls[0][1] as RequestInit;
    const stored = sessionStorage.getItem(sessionStorage.key(0)!);
    await vi.advanceTimersByTimeAsync(15_000);
    const result = await pending;
    expect(result.statusText).toContain("Request outcome unknown: Request timed out after 15 seconds");
    expect(result.complete).toBe(false);
    expect(firstRequest.signal!.aborted).toBe(true);
    expect(sessionStorage.getItem(sessionStorage.key(0)!)).toBe(stored);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(0);

    expect((await closePosition(position, "retry reason")).operationId).toBe(operationId);
    const retry = fetchMock.mock.calls[1][1] as RequestInit;
    expect(retry.body).toBe(firstRequest.body);
    expect(new Headers(retry.headers).get("X-Request-Id")).toBe(new Headers(firstRequest.headers).get("X-Request-Id"));
    expect(JSON.parse(String(retry.body)).client_ref).toBe(JSON.parse(String(firstRequest.body)).client_ref);
    expect(retry.signal!.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("times out a status lookup and retries the saved operation without another POST", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ intent_id: operationId, status: "accepted" }))
      .mockReturnValueOnce(new Promise<Response>(() => {}))
      .mockResolvedValueOnce(response({ status: "queued" }));
    vi.stubGlobal("fetch", fetchMock);
    await closePosition(position, "close");
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(0);
    const stored = sessionStorage.getItem(sessionStorage.key(0)!);
    const pending = queryPositionOperation(operationId);
    await vi.advanceTimersByTimeAsync(15_000);
    const result = await pending;
    expect(result.statusText).toContain("Status unavailable: Request timed out after 15 seconds");
    expect(result.operationId).toBe(operationId);
    expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(true);
    expect(sessionStorage.getItem(sessionStorage.key(0)!)).toBe(stored);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(0);

    expect((await closePosition(position, "retry")).operationStatus).toBe("queued");
    expect(fetchMock.mock.calls[2][0]).toBe(`/v1/operator/orders/${operationId}`);
    expect(fetchMock.mock.calls[2][1].method).toBeUndefined();
    expect(fetchMock.mock.calls.filter(([, init]) => init.method === "POST")).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("sends real account, symbol, side and target with correct action fields", async () => {
    const fetchMock = vi.fn(async () => response({ intent_id: operationId, status: "accepted" }));
    vi.stubGlobal("fetch", fetchMock);
    for (const [submit, expected] of [
      [() => closePosition(position, "close one book"), { action: "close_position" }],
      [() => partialClosePosition(position, 0.125, "reduce exposure"), { action: "partial_close", quantity: 0.125 }],
      [() => moveStopLoss(position, 65000, "adjust protection"), { action: "move_stop_loss", stop_loss: 65000 }]
    ] as const) {
      const result = await submit();
      const [url, request] = fetchMock.mock.calls.at(-1) as unknown as [string, RequestInit];
      const body = JSON.parse(String(request.body));
      expect(url).toBe("/v1/operator/orders");
      expect(request.method).toBe("POST");
      expect(body).toMatchObject({ ...expected, account_id: "account-c", symbol: "BTCUSDT", side: "short", position_side: "short", target_position_id: position.id, authorized_by_type: "user" });
      expect(body).not.toHaveProperty("actor");
      expect(body).not.toHaveProperty("authorized_by_id");
      expect(body).not.toHaveProperty("args");
      expect(body.client_ref).toMatch(/^dashboard-position-[0-9a-f-]{36}$/);
      expect(new Headers(request.headers).get("X-Request-Id")).toBe(body.client_ref.slice("dashboard-position-".length));
      expect(result.complete).toBe(false);
      expect(result.statusText).toBe("Request accepted — awaiting execution");
      expect(result.operationId).toBe(operationId);
    }
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("preserves request body and client_ref across unknown outcomes and module reload", async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error("connection reset after POST"))
      .mockResolvedValueOnce(response({ intent_id: operationId }))
      .mockResolvedValueOnce(response({ intent: { intent_id: operationId, status: "approved" }, status: "queued" }));
    vi.stubGlobal("fetch", fetchMock);
    expect((await closePosition(position, "initial reason")).statusText).toContain("outcome unknown");
    const firstBody = fetchMock.mock.calls[0][1].body;
    vi.resetModules();
    const reloaded = await import("../utils/api");
    await reloaded.closePosition(position, "retry with revised reason");
    expect(fetchMock.mock.calls[1][1].body).toBe(firstBody);
    const repeated = await reloaded.closePosition(position, "another click after acceptance");
    expect(fetchMock.mock.calls[2][0]).toBe(`/v1/operator/orders/${operationId}`);
    expect(fetchMock.mock.calls[2][1].method).toBeUndefined();
    expect(repeated.statusText).toContain("awaiting execution");
    expect(sessionStorage.length).toBe(1);
  });

  it("queries a known operation, preserves rejection reason, and only clears on terminal evidence", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ intent_id: operationId, status: "accepted" }))
      .mockResolvedValueOnce(response({ status: "partial" }))
      .mockResolvedValueOnce(response({ status: "rejected", denial_reason: "position_generation_stale" }));
    vi.stubGlobal("fetch", fetchMock);
    await closePosition(position, "close");
    expect((await queryPositionOperation(operationId)).statusText).toContain("Partially filled");
    expect(sessionStorage.length).toBe(1);
    const rejected = await queryPositionOperation(operationId);
    expect(rejected.statusText).toBe("Request rejected: position_generation_stale");
    expect(rejected.complete).toBe(false);
    expect(sessionStorage.length).toBe(0);
  });

  it("coalesces simultaneous clicks for the same operation", async () => {
    let finish: (value: Response) => void = () => {};
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { finish = resolve; }));
    vi.stubGlobal("fetch", fetchMock);
    const first = closePosition(position, "close");
    const second = closePosition(position, "clicked again");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    finish(response({ intent_id: operationId }));
    expect((await first).operationId).toBe(operationId);
    expect((await second).operationId).toBe(operationId);
  });

  it.each([
    [{ ...position, accountId: "" }, "account ID"],
    [{ ...position, id: "" }, "position ID"],
    [{ ...position, side: "" }, "position side"],
    [{ ...position, pair: "UNAVAILABLE" }, "valid USDT symbol"]
  ])("blocks missing scope without defaulting account, side or symbol", async (target, detail) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    expect((await closePosition(target, "close")).statusText).toContain(detail);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(sessionStorage.length).toBe(0);
  });

  it("does not invent missing position identity or side when mapping either page", async () => {
    const fetchMock = vi.fn(async (url: string) => response(url.startsWith("/v1/positions") ? {
      positions: [{ account_id: "account-d", instrument_id: "BTCUSDT-PERP.BINANCE", quantity: "2" }]
    } : {}));
    vi.stubGlobal("fetch", fetchMock);
    const overview = await fetchDashboardOverview();
    const center = await fetchOrderCenter();
    for (const target of [overview.positions[0], center.positions[0]]) {
      expect(target.accountId).toBe("account-d");
      expect(target.id).toBe("");
      expect(target.side).toBe("");
      expect((await closePosition(target, "close")).statusText).toContain("position ID");
    }
    expect(fetchMock.mock.calls.every(([url]) => url !== "/v1/operator/orders")).toBe(true);
  });

  it("shows HTTP rejection details and preserves the request for uncertain or failed retries", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ detail: "target_position_id does not match server-resolved position" }, 409))
      .mockResolvedValueOnce(response({ detail: "database unavailable" }, 503));
    vi.stubGlobal("fetch", fetchMock);
    const rejected = await closePosition(position, "close");
    expect(rejected.statusText).toContain("HTTP 409");
    expect(rejected.statusText).toContain("target_position_id does not match");
    expect(sessionStorage.key(0)).toContain(POSITION_OPERATION_STORAGE_PREFIX);
    const unknown = await closePosition(position, "close");
    expect(unknown.statusText).toContain("outcome unknown (HTTP 503)");
    expect(fetchMock.mock.calls[1][1].body).toBe(fetchMock.mock.calls[0][1].body);
  });

  it("blocks before POST when stable request storage is unavailable", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("storage denied"); });
    const result = await closePosition(position, "close");
    expect(result.statusText).toContain("submission blocked");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
