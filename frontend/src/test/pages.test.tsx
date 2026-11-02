// Opens every page against the answers reef-web gave in its own tests (src/test/api/*.json, written by the backend's
// tests and copied in by the image build), so the pages are checked against the real API, not a guess of it.
import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import analysis from './api/analysis.json';
import conditions from './api/conditions.json';
import events from './api/events.json';
import history from './api/history.json';
import journal from './api/journal.json';
import me from './api/me.json';
import overview from './api/overview.json';
import program from './api/program.json';
import programs from './api/programs.json';
import reactor from './api/reactor.json';
import report from './api/report.json';
import reports from './api/reports.json';
import switchHistory from './api/history-switch.json';
import tests from './api/tests.json';

const ANSWERS: Record<string, unknown> = {
  'GET /me': me,
  'GET /overview': overview,
  'GET /history': history,
  'GET /events': events,
  'GET /conditions': conditions,
  'GET /tests': tests,
  'GET /programs': programs,
  'GET /analysis': analysis,
  'GET /journal': journal,
  'GET /reports': reports,
  'GET /reactor': reactor,
};

let posts: unknown[] = [];
let calls: string[] = [];
let postAnswers: { status: number; body: unknown }[] = [];

function reply(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function fakeFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(String(input), 'https://reef.example.test');
  const path = url.pathname.replace(/^\/reef\/api/, '');
  const call = `${init?.method ?? 'GET'} ${path}`;
  calls.push(`${call}${url.search}`);
  if (call === 'POST /tests') {
    posts.push(JSON.parse(String(init?.body)));
    const next = postAnswers.shift() ?? { status: 201, body: (tests as { tests: unknown[] }).tests[0] };
    return Promise.resolve(reply(next.status, next.body));
  }
  if (call.startsWith('GET /programs/')) return Promise.resolve(reply(200, program));
  if (call.startsWith('GET /reports/')) return Promise.resolve(reply(200, report));
  if (call === 'GET /history' && url.searchParams.get('sensor') === 'Leak1B') return Promise.resolve(reply(200, switchHistory));
  if (call in ANSWERS) return Promise.resolve(reply(200, ANSWERS[call]));
  return Promise.resolve(reply(404, { detail: `no answer for ${call}` }));
}

function open(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
}

beforeEach(() => {
  posts = [];
  calls = [];
  postAnswers = [];
  vi.stubGlobal('fetch', vi.fn(fakeFetch));
});

afterEach(() => vi.unstubAllGlobals());

describe('the pages, against the API answers of the backend tests', () => {
  it('shows the tank now', async () => {
    open('/');
    expect(await screen.findByText('Temperature')).toBeTruthy();
    expect(screen.getAllByText(/Leak sensor Leak1B/).length).toBeGreaterThan(0);
    expect(screen.getByText('Manual overrides')).toBeTruthy();
    expect(screen.getByText('If Tmp > 82.0 Then OFF')).toBeTruthy(); // a line the whiteLEDR override ignores
    expect(screen.getAllByText(/critical/).length).toBeGreaterThan(0); // the leak alert of the backend fixture
    expect(screen.getByText(/AC4:12345/)).toBeTruthy();
  });

  it('draws a probe and lists a switch on the charts page', async () => {
    open('/charts');
    expect(await screen.findByText(/step \d+ min/)).toBeTruthy();
    expect(calls.some((c) => c.startsWith('GET /history?sensor=Tmp&range=7d'))).toBe(true);
  });

  it('lists the tests and questions an implausible value before storing it', async () => {
    postAnswers = [
      { status: 422, body: { detail: { question: '81 dKH is outside what is plausible for alkalinity (2 to 20 dKH).' } } },
      { status: 201, body: (tests as { tests: unknown[] }).tests[0] },
    ];
    open('/tests');
    expect(await screen.findByText('Enter a test')).toBeTruthy();
    expect(screen.getAllByText('Alkalinity').length).toBeGreaterThan(0);
    fireEvent.change(screen.getByLabelText(/^Value/), { target: { value: '81' } });
    fireEvent.click(screen.getByText('Save test'));
    expect(await screen.findByText(/outside what is plausible/)).toBeTruthy();
    fireEvent.click(screen.getByText('Store anyway'));
    await waitFor(() => expect(posts.length).toBe(2));
    expect(posts[0]).toMatchObject({ parameter: 'alkalinity', value: 81, unit: 'dKH', unusual_ok: false });
    expect(posts[1]).toMatchObject({ unusual_ok: true });
    expect(await screen.findByText(/^Saved:/)).toBeTruthy();
  });

  it('shows the program versions and the differences', async () => {
    open('/programs');
    expect(await screen.findByText('first version read')).toBeTruthy();
    expect(await screen.findByText(/Version of/)).toBeTruthy();
    expect(screen.getByText('All programs in this version')).toBeTruthy();
  });

  it('shows the analysis: findings, the week against the expected band, each probe', async () => {
    open('/analysis');
    expect(await screen.findByText(/Temperature unusually high/)).toBeTruthy();
    expect(screen.getByText('Last week against what was expected')).toBeTruthy();
    expect(screen.getAllByText(/learning/).length).toBeGreaterThan(0); // probes with too little history say so
  });

  it('lists the journal and offers the entry form', async () => {
    open('/journal');
    expect(await screen.findByText('Write an entry')).toBeTruthy();
    expect(screen.getByText(/20 % water change/)).toBeTruthy();
  });

  it('shows the weekly reports', async () => {
    open('/reports');
    expect(await screen.findByText('The 7 days up to now')).toBeTruthy();
    expect((await screen.findAllByText(/test due/)).length).toBeGreaterThan(0);
    expect(screen.getByText('Energy')).toBeTruthy();
  });

  it('shows what each EnergyBar uses', async () => {
    open('/');
    expect((await screen.findAllByText(/kWh a day/)).length).toBeGreaterThan(0);
  });

  it('shows the reactor: CO2 time, effluent pH, tests and the balance note', async () => {
    open('/reactor');
    expect(await screen.findByText(/CO2 on, minutes a day/)).toBeTruthy();
    expect(screen.getByText('Do calcium and alkalinity move together?')).toBeTruthy();
    expect(screen.getAllByText(/dKH/).length).toBeGreaterThan(0);
  });

  it('lists the events', async () => {
    open('/events');
    expect(await screen.findByText('Apex power')).toBeTruthy();
  });
});

