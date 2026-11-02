// The weekly reports the worker makes (site.yaml report: day and hour), and a preview of the 7 days up to now. Each is
// a headline and plain sections: alerts, findings, probes, tests and what is due, energy, programs, journal, the Apex.
import { Button, Card, Group, Stack, Table, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import type { Report, ReportSummary } from '../api/types';
import { Loaded, PageHeader } from '../components/ui';
import { clock } from '../lib/format';

function period(r: { period_start: number; period_end: number }): string {
  const day = (ts: number) => new Date(ts * 1000).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  return `${day(r.period_start)} – ${day(r.period_end - 1)}`;
}

export function ReportsPage() {
  const list = useQuery({ queryKey: ['reports'], queryFn: api.reports, refetchInterval: 1_800_000 });
  return (
    <>
      <PageHeader title="Reports" subtitle="A page on how the tank did each week; its headline is mailed once (Alertmanager)." />
      <Loaded query={list}>{(data) => <Reports reports={data.reports} />}</Loaded>
    </>
  );
}

function Reports({ reports }: { reports: ReportSummary[] }) {
  const [selected, setSelected] = useState<number | 'preview'>(reports[0]?.id ?? 'preview');
  const report = useQuery({ queryKey: ['report', selected], queryFn: () => api.report(selected) });
  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Text size="sm" c="dimmed">
          {reports.length === 0 ? 'No weekly report yet: the first comes on the report day.' : `${reports.length} kept.`}
        </Text>
        <Button variant={selected === 'preview' ? 'filled' : 'light'} onClick={() => setSelected('preview')}>
          The 7 days up to now
        </Button>
      </Group>
      {reports.length > 0 && (
        <Card padding={0}>
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Week</Table.Th>
                <Table.Th>Headline</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {reports.map((r) => (
                <Table.Tr
                  key={r.id}
                  className="clickable"
                  bg={r.id === selected ? 'var(--mantine-primary-color-light)' : undefined}
                  onClick={() => setSelected(r.id)}
                >
                  <Table.Td className="nowrap">{period(r)}</Table.Td>
                  <Table.Td>{r.headline}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Card>
      )}
      <Loaded query={report}>{(r) => <ReportView report={r} />}</Loaded>
    </Stack>
  );
}

function ReportView({ report }: { report: Report }) {
  return (
    <Card padding="md">
      <Group justify="space-between" mb="xs" wrap="wrap">
        <Title order={4}>{period(report)}</Title>
        <Text size="sm" c="dimmed">
          {report.id === null ? `a preview, made ${clock(report.ts)}` : `made ${clock(report.ts)}`}
        </Text>
      </Group>
      <Text fw={600} mb="sm">
        {report.headline}
      </Text>
      <Stack gap="sm">
        {report.sections.map((section) => (
          <div key={section.title}>
            <Text size="sm" fw={600} c="dimmed" tt="uppercase">
              {section.title}
            </Text>
            {section.lines.map((line, n) => (
              <Text key={n} size="sm">
                {line}
              </Text>
            ))}
          </div>
        ))}
      </Stack>
    </Card>
  );
}
