/**
 * Tests for the outbound proxy wrapper.
 */
import { describe, it, expect } from "bun:test";
import { createProxiedFetch } from "./outbound-proxy.js";

describe("createProxiedFetch", () => {
  it("returns the base fetch unchanged when no proxy is configured", () => {
    const base = (async () => new Response("ok")) as typeof fetch;
    const wrapped = createProxiedFetch(undefined, base);
    expect(wrapped).toBe(base);
  });

  it("returns the base fetch unchanged when proxy.url is empty", () => {
    const base = (async () => new Response("ok")) as typeof fetch;
    const wrapped = createProxiedFetch({ url: "" }, base);
    expect(wrapped).toBe(base);
  });

  it("injects the proxy option into every request", async () => {
    let capturedInit: RequestInit | undefined;
    const base = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      capturedInit = init;
      return new Response("ok");
    }) as typeof fetch;

    const wrapped = createProxiedFetch(
      { url: "socks5://127.0.0.1:1080" },
      base,
    );
    await wrapped("https://example.com");

    expect((capturedInit as any)?.proxy).toBe("socks5://127.0.0.1:1080");
  });

  it("preserves other init fields alongside the injected proxy", async () => {
    let capturedInit: RequestInit | undefined;
    const base = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      capturedInit = init;
      return new Response("ok");
    }) as typeof fetch;

    const wrapped = createProxiedFetch(
      { url: "http://127.0.0.1:8080" },
      base,
    );
    await wrapped("https://example.com", {
      method: "POST",
      headers: { "x-test": "1" },
    });

    expect(capturedInit?.method).toBe("POST");
    expect((capturedInit?.headers as Record<string, string>)?.["x-test"]).toBe(
      "1",
    );
    expect((capturedInit as any)?.proxy).toBe("http://127.0.0.1:8080");
  });
});
