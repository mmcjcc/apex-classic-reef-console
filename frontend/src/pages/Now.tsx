// The tank now: alerts, probes with 24 hours, floats and leak sensor, EnergyBars and modules, manual overrides with
// the program lines they ignore, and every output.
import { Accordion, Alert, Badge, Card, Group, List, SimpleGrid, Stack, Table, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { Overview } from '../api/types';
import { Loaded, ModeBadge, Section, Since, Spark } from '../components/ui';
import { clock, reading, span, unitText } from '../lib/format';

export function NowPage() {
  const query = useQuery({ queryKey: ['overview'], queryFn: api.overview, refetchInterval: 30_000 });
  return <Loaded query={query}>{(data) => <Now data={data} />}</Loaded>;
}

function Now({ data }: { data: Overview }) {
  if (!data.apex) {
    return <Alert title="No reading yet">The worker has not read the Apex yet. It polls once a minute.</Alert>;
  }
  return (
    <Stack gap="lg">
      <Alerts data={data} />
      <Section title="Water">
        <SimpleGrid cols={{ base: 2, sm: 3, lg: 4 }} spacing="sm">
          {data.probes.map((p) => (
            <Card key={p.name} padding="sm" className={p.alarm ? 'alarm' : undefined}>
              <Text size="sm" c="dimmed">
                {p.label}
              </Text>
              <Text className="value">
                {reading(p.value, p.unit)}{' '}
                <Text span size="md" c="dimmed" fw={500}>
                  {unitText(p.unit)}
                </Text>
              </Text>
              <Text size="xs" c="dimmed">
                24 h: {span(p.history, p.unit)}
              </Text>
              <Spark points={p.history} color={p.alarm ? 'red' : 'cyan'} />
            </Card>
          ))}
        </SimpleGrid>
      </Section>
      <Section title="Floats and leak sensor">
        <SimpleGrid cols={{ base: 2, sm: 3, lg: 5 }} spacing="sm">
          {data.switches.map((s) => (
            <Card key={s.name} padding="sm" className={s.alarm ? 'alarm' : undefined}>
              <Group justify="space-between" wrap="nowrap">
                <Text size="sm">{s.label}</Text>
                <Badge color={s.alarm ? 'red' : 'gray'} variant={s.alarm ? 'filled' : 'light'} style={{ flexShrink: 0 }}>
                  {s.closed ? 'CLOSED' : 'OPEN'}
                </Badge>
              </Group>
            </Card>
          ))}
        </SimpleGrid>
      </Section>
      <Section title="EnergyBars and modules">
        <SimpleGrid cols={{ base: 1, xs: 2, lg: 3 }} spacing="sm">
          {data.modules.map((m) => (
            <Card key={m.id} padding="sm" className={m.connected && !m.alarm ? undefined : 'alarm'}>
              <Group justify="space-between" wrap="nowrap">
                <Text fw={600}>
                  {m.id}{' '}
                  <Text span size="sm" c="dimmed" fw={400}>
                    {m.kind}, AquaBus {m.address}
                  </Text>
                </Text>
                <Badge color={m.connected ? 'teal' : 'red'} variant={m.connected ? 'light' : 'filled'} style={{ flexShrink: 0 }}>
                  {m.connected ? 'reporting' : 'lost'}
                </Badge>
              </Group>
              {!m.connected && (
                <Text size="sm" c="red">
                  {m.why}
                </Text>
              )}
              {m.amps !== null && (
                <>
                  <Text className="value">
                    {reading(m.amps, 'A')}{' '}
                    <Text span size="md" c="dimmed" fw={500}>
                      A
                    </Text>
                  </Text>
                  <Text size="xs" c="dimmed">
                    24 h: {span(m.history, 'A')}
                  </Text>
                  <Spark points={m.history} />
                  <EnergyLine data={data} module={m.id} />
                </>
              )}
            </Card>
          ))}
        </SimpleGrid>
        <EnergyTotal data={data} />
      </Section>
      <Section title="Manual overrides" hint="An output switched ON or OFF by hand ignores its whole program, including the lines below.">
        {data.overrides.length === 0 ? (
          <Text c="dimmed">None: every output follows its program.</Text>
        ) : (
          <Card padding={0}>
            <Table.ScrollContainer minWidth={560}>
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Output</Table.Th>
                    <Table.Th>Held</Table.Th>
                    <Table.Th>Seen since</Table.Th>
                    <Table.Th>Program lines it ignores</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {data.overrides.map((o) => (
                    <Table.Tr key={o.name}>
                      <Table.Td ff="monospace">{o.name}</Table.Td>
                      <Table.Td>
                        <Badge color="yellow" variant="light">
                          Manual {o.mode}
                        </Badge>
                      </Table.Td>
                      <Table.Td className="nowrap">
                        {o.since && data.polling_since && o.since - data.polling_since < 120 ? `before ${clock(data.polling_since)}` : clock(o.since)}
                      </Table.Td>
                      <Table.Td>
                        {!o.program_known ? (
                          <Text c="dimmed" size="sm">
                            program not read yet
                          </Text>
                        ) : o.ignored.length === 0 ? (
                          <Text c="dimmed" size="sm">
                            only its schedule and defaults
                          </Text>
                        ) : (
                          <List size="sm" ff="monospace">
                            {o.ignored.map((line) => (
                              <List.Item key={line}>{line}</List.Item>
                            ))}
                          </List>
                        )}
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          </Card>
        )}
      </Section>
      <Section title="Outputs">
        <Accordion variant="separated" multiple defaultValue={data.groups.filter((g) => g.id !== 'virtual').map((g) => g.id)}>
          {data.groups.map((g) => (
            <Accordion.Item key={g.id} value={g.id}>
              <Accordion.Control>{g.title}</Accordion.Control>
              <Accordion.Panel>
                {g.id === 'virtual' && (
                  <Text size="sm" c="dimmed" mb="xs">
                    Virtual outlets switch no socket.{g.unused ? ` ${g.unused} of them are used by no program.` : ''}
                  </Text>
                )}
                <SimpleGrid cols={{ base: 1, xs: 2, md: 3, xl: 4 }} spacing={6} verticalSpacing={6}>
                  {g.outputs.map((o) => (
                    <Group key={o.name} justify="space-between" wrap="nowrap" gap="xs">
                      <Text size="sm" ff="monospace" truncate title={o.did}>
                        {o.name}
                      </Text>
                      <Group gap={4} wrap="nowrap">
                        {o.health && o.health !== 'OK' && <Badge color="red">{o.health}</Badge>}
                        <ModeBadge output={o} />
                      </Group>
                    </Group>
                  ))}
                </SimpleGrid>
              </Accordion.Panel>
            </Accordion.Item>
          ))}
        </Accordion>
      </Section>
      <Text size="xs" c="dimmed">
        Readings every minute from the Apex {data.apex.serial} (firmware {data.apex.software}). History since {clock(data.history_since)}.
        Times are in this device's time zone; the Apex's own clock is set to UTC {data.apex.timezone}.
        {data.apex.feed_active ? ' A Feed cycle is running.' : ''}
      </Text>
    </Stack>
  );
}

/** A bar's energy over the last 7 days: kWh a day, and the cost a month once the tariff is set. */
function EnergyLine({ data, module }: { data: Overview; module: string }) {
  const bar = data.energy?.bars.find((b) => b.module === module);
  if (!bar) return null;
  return (
    <Text size="xs" c="dimmed" mt={4}>
      7 days: {bar.kwh_per_day.toFixed(1)} kWh a day{bar.cost_per_month !== null ? `, about $${bar.cost_per_month.toFixed(2)} a month` : ''}
    </Text>
  );
}

function EnergyTotal({ data }: { data: Overview }) {
  const bars = data.energy?.bars ?? [];
  if (bars.length === 0) return null;
  const kwh = bars.reduce((sum, b) => sum + b.kwh_per_day, 0);
  const price = data.energy.price_per_kwh;
  return (
    <Text size="xs" c="dimmed" mt="xs">
      All EnergyBars: {kwh.toFixed(1)} kWh a day
      {price !== null ? `, about $${(kwh * 30.4 * price).toFixed(2)} a month` : ' (set energy.price_per_kwh in site.yaml for the cost)'}, at{' '}
      {data.energy.volts} V and a power factor taken as 1.
    </Text>
  );
}

function Alerts({ data }: { data: Overview }) {
  if (data.alerts.length === 0) {
    return (
      <Alert color="teal" variant="light" title="No alerts">
        Every condition is within its limits.
      </Alert>
    );
  }
  return (
    <Card className="alarm" padding="sm">
      <Stack gap={6}>
        {data.alerts.map((a) => (
          <Group key={a.id} gap="xs" wrap="wrap" align="baseline">
            <Badge color={a.severity === 'critical' ? 'red' : 'yellow'} variant="filled">
              {a.severity}
            </Badge>
            <Text fw={600}>{a.text}</Text>
            <Text size="sm" c="dimmed">
              {a.detail ? `${a.detail} · ` : ''}since {clock(a.since)} (<Since ts={a.since} now={data.now} />)
            </Text>
          </Group>
        ))}
      </Stack>
    </Card>
  );
}
