// Manual water tests: the newest of each parameter with its trend, the entry form (agent and admin sign-ins), and
// every test. The time is always explicit in the form, so what is stored is what the person saw; a value outside the
// plausible range is questioned and stored only after a second click.
import { Sparkline } from '@mantine/charts';
import { Alert, Anchor, Button, Card, Group, NumberInput, Select, SimpleGrid, Stack, Table, Text, TextInput } from '@mantine/core';
import { notifications } from '@mantine/notifications';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { ApiError, api } from '../api/client';
import type { Me, Parameter, TestsOut, WaterTest, WaterTestIn } from '../api/types';
import { Loaded, PageHeader, Section, Since, lifted } from '../components/ui';
import { clock, localInput, longClock } from '../lib/format';

export function TestsPage({ me }: { me: Me }) {
  const query = useQuery({ queryKey: ['tests'], queryFn: () => api.tests(), refetchInterval: 300_000 });
  return (
    <>
      <PageHeader title="Water tests" subtitle="Values in each parameter's own unit; the entered value and unit are kept too." />
      <Loaded query={query}>{(data) => <Tests data={data} me={me} />}</Loaded>
    </>
  );
}

function shown(t: WaterTest, p: Parameter | undefined): string {
  const decimals = p?.decimals ?? 2;
  const main = `${t.value.toFixed(decimals)} ${t.unit === 'pH' ? '' : t.unit}`.trim();
  return t.entered_unit !== t.unit ? `${main} (entered ${t.entered_value} ${t.entered_unit})` : main;
}

function Tests({ data, me }: { data: TestsOut; me: Me }) {
  const byId = Object.fromEntries(data.parameters.map((p) => [p.id, p]));
  const queryClient = useQueryClient();
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteTest(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['tests'] }),
    onError: (error: Error) => notifications.show({ color: 'red', title: 'Not deleted', message: error.message }),
  });
  return (
    <Stack gap="lg">
      {data.latest.length === 0 ? (
        <Text c="dimmed">No water tests yet.</Text>
      ) : (
        <SimpleGrid cols={{ base: 2, sm: 3, lg: 4 }} spacing="sm">
          {data.latest.map((t) => {
            const p = byId[t.parameter];
            const decimals = p?.decimals ?? 2;
            const change = t.previous ? t.value - t.previous.value : null;
            const series = data.tests
              .filter((x) => x.parameter === t.parameter)
              .map((x) => x.value)
              .reverse();
            return (
              <Card key={t.parameter} padding="sm">
                <Text size="sm" c="dimmed">
                  {t.label}
                </Text>
                <Text className="value">
                  {t.value.toFixed(decimals)}{' '}
                  <Text span size="md" c="dimmed" fw={500}>
                    {t.unit === 'pH' ? '' : t.unit}
                  </Text>
                </Text>
                <Text size="xs" c="dimmed">
                  <Since ts={t.measured_at} />
                  {change !== null && t.previous
                    ? ` · ${change >= 0 ? '+' : ''}${change.toFixed(decimals)} since ${clock(t.previous.measured_at)}`
                    : ''}
                </Text>
                {series.length > 1 && <Sparkline h={34} mt={6} data={lifted(series)} color="cyan" curveType="linear" fillOpacity={0.15} />}
              </Card>
            );
          })}
        </SimpleGrid>
      )}
      {me.can_write && <TestForm parameters={data.parameters} />}
      <Section title="Every test">
        {data.tests.length === 0 ? (
          <Text c="dimmed">Nothing entered yet.</Text>
        ) : (
          <Card padding={0}>
            <Table.ScrollContainer minWidth={640}>
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Measured</Table.Th>
                    <Table.Th>Parameter</Table.Th>
                    <Table.Th>Value</Table.Th>
                    <Table.Th>Kit</Table.Th>
                    <Table.Th>Note</Table.Th>
                    <Table.Th>Entered by</Table.Th>
                    {me.can_write && <Table.Th />}
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {data.tests.map((t) => {
                    const p = byId[t.parameter];
                    return (
                      <Table.Tr key={t.id}>
                        <Table.Td className="nowrap">{longClock(t.measured_at)}</Table.Td>
                        <Table.Td>{p?.label ?? t.parameter}</Table.Td>
                        <Table.Td>{shown(t, p)}</Table.Td>
                        <Table.Td>
                          {t.kit}
                          {t.kit_error !== null ? ` ±${t.kit_error.toFixed(p?.decimals ?? 2)}` : ''}
                        </Table.Td>
                        <Table.Td>{t.note}</Table.Td>
                        <Table.Td c="dimmed">
                          {t.entered_by} ({t.source})
                        </Table.Td>
                        {me.can_write && (
                          <Table.Td>
                            <Anchor
                              component="button"
                              size="sm"
                              onClick={() => {
                                if (window.confirm('Delete this test? It stays in the database, marked deleted.')) remove.mutate(t.id);
                              }}
                            >
                              delete
                            </Anchor>
                          </Table.Td>
                        )}
                      </Table.Tr>
                    );
                  })}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          </Card>
        )}
      </Section>
    </Stack>
  );
}

function TestForm({ parameters }: { parameters: Parameter[] }) {
  const queryClient = useQueryClient();
  const [parameter, setParameter] = useState(parameters[0]?.id ?? 'alkalinity');
  const current = parameters.find((p) => p.id === parameter);
  const [unit, setUnit] = useState<string | null>(current?.unit ?? null);
  const [value, setValue] = useState<number | string>('');
  const [time, setTime] = useState(localInput(new Date()));
  const [timeEdited, setTimeEdited] = useState(false);
  const [kit, setKit] = useState('');
  const [error, setError] = useState<number | string>('');
  const [note, setNote] = useState('');
  const [question, setQuestion] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  // an installed app stays open for days: until the person sets the time, it follows the clock
  useEffect(() => {
    if (timeEdited) return undefined;
    const tick = () => setTime(localInput(new Date()));
    const timer = window.setInterval(tick, 30_000);
    document.addEventListener('visibilitychange', tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [timeEdited]);

  const save = useMutation({
    mutationFn: (body: WaterTestIn) => api.addTest(body),
    onSuccess: (test) => {
      setQuestion(null);
      setTimeEdited(false);
      setTime(localInput(new Date()));
      setSaved(`Saved: ${current?.label ?? test.parameter} ${shown(test, current)}, measured ${longClock(test.measured_at)}.`);
      setValue('');
      setNote('');
      void queryClient.invalidateQueries({ queryKey: ['tests'] });
    },
    onError: (err: Error) => {
      setSaved(null);
      setQuestion(err instanceof ApiError && err.question ? err.question : null);
      if (!(err instanceof ApiError && err.question)) notifications.show({ color: 'red', title: 'Not saved', message: err.message });
    },
  });

  const submit = (unusual: boolean) => {
    const measured = new Date(time).getTime() / 1000;
    const number = value === '' ? NaN : Number(value);
    if (!Number.isFinite(number) || !Number.isFinite(measured)) {
      notifications.show({ color: 'red', title: 'Not saved', message: 'A value and a time are needed.' });
      return;
    }
    save.mutate({
      parameter,
      value: number,
      unit,
      measured_at: measured,
      kit,
      kit_error: error === '' || !Number.isFinite(Number(error)) ? null : Number(error),
      note,
      unusual_ok: unusual,
    });
  };

  return (
    <Card padding="md">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit(false);
        }}
      >
        <Text fw={600} mb="xs">
          Enter a test
        </Text>
        <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }} spacing="sm">
          <Select
            label="Parameter"
            data={parameters.map((p) => ({ value: p.id, label: p.label }))}
            value={parameter}
            allowDeselect={false}
            onChange={(v) => {
              const next = parameters.find((p) => p.id === v);
              setParameter(v ?? parameter);
              setUnit(next?.unit ?? null);
              setQuestion(null);
            }}
          />
          <NumberInput
            label="Value"
            value={value}
            onChange={(v) => {
              setValue(v);
              setQuestion(null);
            }}
            decimalScale={4}
            required
          />
          <Select
            label="Unit"
            data={current?.units ?? []}
            value={unit}
            allowDeselect={false}
            onChange={(v) => {
              setUnit(v);
              setQuestion(null);
            }}
          />
          <TextInput
            label="Measured"
            type="datetime-local"
            value={time}
            onChange={(e) => {
              setTime(e.currentTarget.value);
              setTimeEdited(true);
            }}
            required
          />
          <TextInput label="Kit" placeholder="e.g. Hanna HI772" value={kit} onChange={(e) => setKit(e.currentTarget.value)} maxLength={120} />
          <NumberInput label="Kit error ±" value={error} onChange={setError} min={0} decimalScale={4} />
          <TextInput label="Note" value={note} onChange={(e) => setNote(e.currentTarget.value)} maxLength={500} style={{ gridColumn: 'span 2' }} />
        </SimpleGrid>
        {question && (
          <Alert color="yellow" mt="sm" title="Check this value">
            {question}
          </Alert>
        )}
        {saved && (
          <Text c="teal" size="sm" mt="sm">
            {saved}
          </Text>
        )}
        <Group justify="flex-end" mt="sm">
          {question && (
            <Button variant="light" color="yellow" onClick={() => submit(true)} loading={save.isPending}>
              Store anyway
            </Button>
          )}
          <Button type="submit" loading={save.isPending}>
            Save test
          </Button>
        </Group>
      </form>
    </Card>
  );
}
