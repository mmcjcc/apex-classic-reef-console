// The Apex's programs, read once a day: every version kept, what changed between them, and every program as read.
// Read-only: the programs are edited in Fusion.
import { Accordion, Badge, Card, Group, Stack, Table, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api } from '../api/client';
import type { ProgramVersion, VersionSummary } from '../api/types';
import { Loaded, PageHeader } from '../components/ui';
import { clock, longClock } from '../lib/format';

export function ProgramsPage() {
  const versions = useQuery({ queryKey: ['programs'], queryFn: api.programs, refetchInterval: 1_800_000 });
  return (
    <>
      <PageHeader title="Programs" subtitle="Read from the Apex once a day; a new version is kept whenever something changed (edits are made in Fusion)." />
      <Loaded query={versions}>{(data) => <Versions versions={data.versions} />}</Loaded>
    </>
  );
}

function Versions({ versions }: { versions: VersionSummary[] }) {
  const [selected, setSelected] = useState<number | null>(versions[0]?.id ?? null);
  const detail = useQuery({ queryKey: ['program', selected], queryFn: () => api.program(selected ?? 0), enabled: selected !== null });
  if (versions.length === 0) return <Text c="dimmed">The programs have not been read yet (once a day).</Text>;
  return (
    <Stack gap="md">
      <Card padding={0}>
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Read</Table.Th>
              <Table.Th>What changed</Table.Th>
              <Table.Th>Outputs</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {versions.map((v) => (
              <Table.Tr
                key={v.id}
                className="clickable"
                bg={v.id === selected ? 'var(--mantine-primary-color-light)' : undefined}
                onClick={() => setSelected(v.id)}
              >
                <Table.Td className="nowrap">{clock(v.ts)}</Table.Td>
                <Table.Td>{v.summary}</Table.Td>
                <Table.Td>{v.outputs}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Card>
      {selected !== null && <Loaded query={detail}>{(v) => <Version version={v} />}</Loaded>}
    </Stack>
  );
}

function Version({ version }: { version: ProgramVersion }) {
  return (
    <Card padding="md">
      <Group justify="space-between" mb="sm">
        <Text fw={600}>Version of {longClock(version.ts)}</Text>
        <Text size="sm" c="dimmed">
          {version.previous ? `compared with ${longClock(version.previous.ts)}` : 'the first version read'}
        </Text>
      </Group>
      <Stack gap="sm">
        {version.changes.map((c) => (
          <div key={c.output}>
            <Group gap="xs" mb={4}>
              <Text ff="monospace" fw={600}>
                {c.output}
              </Text>
              <Badge variant="light" color={c.change === 'added' ? 'teal' : c.change === 'removed' ? 'red' : 'yellow'}>
                {c.change}
              </Badge>
            </Group>
            <pre className="diff">
              {c.lines.map(([op, text], n) => (
                <div key={n} className={op === '+' ? 'add' : op === '-' ? 'del' : undefined}>
                  {op} {text}
                </div>
              ))}
            </pre>
          </div>
        ))}
      </Stack>
      <Accordion mt="sm" variant="contained" defaultValue={version.changes.length ? null : 'all'}>
        <Accordion.Item value="all">
          <Accordion.Control>All programs in this version</Accordion.Control>
          <Accordion.Panel>
            <Stack gap="xs">
              {Object.entries(version.programs).map(([name, lines]) => (
                <div key={name}>
                  <Text ff="monospace" fw={600} size="sm">
                    {name}
                  </Text>
                  <pre className="diff">{lines.join('\n')}</pre>
                </div>
              ))}
            </Stack>
          </Accordion.Panel>
        </Accordion.Item>
      </Accordion>
    </Card>
  );
}
