// One small fetch wrapper for reef-web's API (/reef/api). Without a sign-in reef-web answers 401: in basic mode the
// browser asks for the password, in forward mode the X-Sign-In header says where the sign-in page is. A reverse proxy
// in front may instead redirect the request to its sign-in page: then the whole page is reloaded, so the proxy can
// send the browser there.
import type {
  AnalysisOut,
  Condition,
  JournalEntry,
  JournalOut,
  ReactorOut,
  Report,
  ReportSummary,
  EventItem,
  History,
  Me,
  Overview,
  ProgramVersion,
  RangeName,
  TestsOut,
  VersionSummary,
  WaterTest,
  WaterTestIn,
} from './types';

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public question: string | null = null, // a value questioned as implausible: store it only after a confirmation
    public signIn: string | null = null, // where to sign in (forward mode), on a 401
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/reef/api${path}`, {
    method,
    credentials: 'same-origin',
    // the custom header is the CSRF check: browsers do not add it to cross-site requests
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'reef-console' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.redirected && !(response.headers.get('Content-Type') ?? '').includes('json')) {
    window.location.reload(); // a proxy sent the request to its sign-in page: let it send the page there
    throw new ApiError(401, 'signed out');
  }
  if (!response.ok) {
    let detail = response.statusText;
    let question: string | null = null;
    try {
      const payload = await response.json();
      if (typeof payload.detail === 'string') detail = payload.detail;
      else if (payload.detail && typeof payload.detail.question === 'string') detail = question = payload.detail.question;
      else if (Array.isArray(payload.detail)) detail = payload.detail.map((d: { msg: string }) => d.msg).join('; ');
    } catch {
      // not JSON: keep the status text
    }
    throw new ApiError(response.status, detail, question, response.headers.get('X-Sign-In'));
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

export const api = {
  me: () => request<Me>('GET', '/me'),
  overview: () => request<Overview>('GET', '/overview'),
  history: (sensor: string, range: RangeName) => request<History>('GET', `/history${query({ sensor, range })}`),
  events: (range: RangeName, kind?: string) => request<{ events: EventItem[] }>('GET', `/events${query({ range, kind })}`),
  conditions: () => request<{ conditions: Condition[] }>('GET', '/conditions'),
  tests: (days = 365) => request<TestsOut>('GET', `/tests${query({ days, limit: 500 })}`),
  addTest: (body: WaterTestIn) => request<WaterTest>('POST', '/tests', body),
  deleteTest: (id: number) => request<void>('DELETE', `/tests/${id}`),
  analysis: () => request<AnalysisOut>('GET', '/analysis'),
  journal: (days = 365) => request<JournalOut>('GET', `/journal${query({ days })}`),
  addJournal: (body: { ts: number; kind: string; text: string }) => request<JournalEntry>('POST', '/journal', body),
  deleteJournal: (id: number) => request<void>('DELETE', `/journal/${id}`),
  reactor: (days = 30) => request<ReactorOut>('GET', `/reactor${query({ days })}`),
  reports: () => request<{ reports: ReportSummary[] }>('GET', '/reports'),
  report: (id: number | 'preview') => request<Report>('GET', `/reports/${id}`),
  programs: () => request<{ versions: VersionSummary[] }>('GET', '/programs'),
  program: (id: number) => request<ProgramVersion>('GET', `/programs/${id}`),
};
