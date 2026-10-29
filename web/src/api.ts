import type { Bootstrap, Overview, Role, Session } from "./types";
const KEY = "medplain.session.v2";
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export function token(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null;
  }
}
function saveToken(value: string) {
  try {
    localStorage.setItem(KEY, value);
  } catch {
    /* Session still works in memory. */
  }
  memoryToken = value;
}
let memoryToken = token();
export async function api<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  const timeout = window.setTimeout(abort, 25000);
  try {
    const response = await fetch(`/api/ops${path}`, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(memoryToken || token()
          ? { "X-Medplain-Session": memoryToken || token() || "" }
          : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => null);
    if (!response.ok)
      throw new ApiError(
        typeof data?.detail === "string"
          ? data.detail
          : `Service request failed (HTTP ${response.status}).`,
        response.status,
      );
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
    throw new ApiError(
      error instanceof DOMException && error.name === "AbortError"
        ? "The API did not respond in time. Read the current state before retrying an action."
        : "The API cannot be reached. Check your connection and retry the read.",
      0,
    );
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}
export async function newSession(preparedDemo = true): Promise<Bootstrap> {
  const created = await api<Session & { session_token: string }>("/session", {
    prepared_demo: preparedDemo,
  });
  saveToken(created.session_token);
  return api<Bootstrap>("/bootstrap");
}
export async function loadExamples(): Promise<Bootstrap> {
  await api<Bootstrap>("/session/demo", {});
  return api<Bootstrap>("/bootstrap");
}
async function readBoot(): Promise<Bootstrap> {
  const explicitEmpty =
    new URLSearchParams(location.search).get("collection") === "empty";
  if (!memoryToken && !token()) return newSession(!explicitEmpty);
  try {
    const data = await api<Bootstrap>("/bootstrap");
    if (
      !explicitEmpty &&
      data.collection &&
      !data.collection.initialized &&
      ["operator", "admin"].includes(data.session.role)
    ) {
      const snapshot = await api<Overview>("/overview?workspace=all");
      if (snapshot.counts.total === 0) return loadExamples();
    }
    return data;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401)
      return newSession(!explicitEmpty);
    throw error;
  }
}
let bootRequest: Promise<Bootstrap> | undefined;
export function boot(): Promise<Bootstrap> {
  if (!bootRequest)
    bootRequest = readBoot().catch((error) => {
      bootRequest = undefined;
      throw error;
    });
  return bootRequest;
}
export async function changeRole(role: Role): Promise<Bootstrap> {
  await api("/session/role", { role });
  return api<Bootstrap>("/bootstrap");
}
export const key = () => crypto.randomUUID();
export const message = (error: unknown) =>
  error instanceof Error ? error.message : "The request failed.";
