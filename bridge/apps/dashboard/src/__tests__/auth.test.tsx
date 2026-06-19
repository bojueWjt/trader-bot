import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";

afterEach(() => {
  cleanup();
  localStorage.clear();
  sessionStorage.clear();
  window.history.pushState({}, "", "/");
  vi.unstubAllGlobals();
});

describe("dashboard auth gate", () => {
  it("shows login page on app bootstrap when no token exists", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("login required")));
    window.history.pushState({}, "", "/dashboard");

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/login");
  });

  it("stores token after successful login and redirects to dashboard", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      if (String(input) === "/api/auth/login") {
        return Promise.resolve({
          json: () => Promise.resolve({
            access_token: "issued-token",
            expires_in: 3600,
            role: "risk_admin",
            token_type: "bearer"
          }),
          ok: true
        });
      }

      return Promise.reject(new Error("offline"));
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("EventSource", undefined);
    window.history.pushState({}, "", "/login");

    render(<App />);

    fireEvent.change(await screen.findByLabelText("Username"), { target: { value: "admin" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(localStorage.getItem("hermes.auth.token")).toBe("issued-token");
    });
    expect(window.location.pathname).toBe("/dashboard");
    expect(await screen.findByText("实时连接断开")).toBeInTheDocument();
  });
});
