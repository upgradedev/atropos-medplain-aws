import type { Bootstrap, Role } from "./types";

declare global {
  interface Window {
    __MEDPLAIN_REPLAY_API_ATTEMPTS__?: Array<{ path: string; method: string; transmitted: false }>;
  }
}
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export function token(): null { return null; }
export async function api<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  (window.__MEDPLAIN_REPLAY_API_ATTEMPTS__ ||= []).push({path,method:body === undefined ? "GET" : "POST",transmitted:false});
  if (signal?.aborted) throw new DOMException("Cancelled", "AbortError");
  throw new ApiError("Historical replay transport is disabled. Reference metadata is unavailable; no request was transmitted.", 0);
}
export function newSession(_preparedDemo = true): Promise<Bootstrap> { return api<Bootstrap>("/session", {}); }
export function loadExamples(): Promise<Bootstrap> { return api<Bootstrap>("/session/demo", {}); }
export function boot(): Promise<Bootstrap> { return api<Bootstrap>("/bootstrap"); }
export function changeRole(_role: Role): Promise<Bootstrap> { return api<Bootstrap>("/session/role", {}); }
export const key = (): string => { throw new Error("Historical replay actions are disabled"); };
export const message = (error: unknown): string => error instanceof Error ? error.message : "Historical replay metadata is unavailable.";
