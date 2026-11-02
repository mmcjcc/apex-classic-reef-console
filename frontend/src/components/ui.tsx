// Shared building blocks. Pages compose these; nothing here knows about a specific page.
import { Sparkline } from '@mantine/charts';
import { Alert, Badge, Center, Group, Loader, SegmentedControl, Text, Title, Tooltip } from '@mantine/core';
import type { UseQueryResult } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import type { Output, Overview, RangeName, Series } from '../api/types';
import { ago, clock } from '../lib/format';

export function PageHeader({ title, subtitle, children }: { title: string; subtitle?: string; children?: ReactNode }) {
  return (
    <Group justify="space-between" align="flex-end" mb="md" wrap="wrap">
      <div>
        <Title order={2}>{title}</Title>
        {subtitle && (
          <Text c="dimmed" size="sm">
            {subtitle}
          </Text>
        )}
      </div>
      <Group gap="sm">{children}</Group>
    </Group>
  );
}

/** Loading, error, or the page: every query-backed page goes through this. */
export function Loaded<T>({ query, children }: { query: UseQueryResult<T>; children: (data: T) => ReactNode }) {
  if (query.isPending) {
    return (
      <Center py="xl">
        <Loader />
      </Center>
    );
  }
  if (query.isError) {
    return (
      <Alert color="red" title="Could not load">
        {query.error.message}
      </Alert>
    );
  }
  return <>{children(query.data)}</>;
}

export function PollBadge({ overview }: { overview: Overview }) {
  const { poll, now } = overview;
  if (poll.current) {
    return (
      <Badge color="teal" variant="light">
        Apex answering · {ago(poll.last_good, now)}
      </Badge>
    );
  }
  return (
    <Tooltip label={poll.error ?? 'no reading yet'} withArrow>
      <Badge color="red" variant="filled">
        {poll.last_good ? `Apex not answering · last ${ago(poll.last_good, now)}` : 'No reading yet'}
      </Badge>
    </Tooltip>
  );
}

/** A sparkline drawn from the series' own low to high: the chart starts at zero, so the values are shifted (it has
 * no axis to mislead), and a day of 78.1-79.6 °F shows its shape instead of a flat line. */
export function Spark({ points, color = 'cyan' }: { points: Series; color?: string }) {
  if (points.length < 2) return null;
  return <Sparkline h={38} mt={6} data={lifted(points.map((p) => p[1]))} color={color} curveType="linear" fillOpacity={0.15} strokeWidth={1.6} />;
}

export function lifted(values: number[]): number[] {
  const low = Math.min(...values);
  const spread = Math.max(...values) - low;
  return spread === 0 ? values.map(() => 1) : values.map((v) => (v - low) / spread + 0.1);
}

export function ModeBadge({ output }: { output: Output }) {
  const { mode, type, intensity, manual, energized } = output;
  if (type === 'variable' && !manual) {
    return (
      <Badge color="blue" variant="light">
        {mode} {intensity ?? ''}%
      </Badge>
    );
  }
  if (manual) {
    return (
      <Badge color="yellow" variant="light">
        Manual {mode}
      </Badge>
    );
  }
  return (
    <Badge color={energized ? 'teal' : 'gray'} variant="light">
      {energized ? 'Auto · on' : 'Auto · off'}
    </Badge>
  );
}

export function Since({ ts, now }: { ts: number | null; now?: number }) {
  return (
    <Tooltip label={clock(ts)} withArrow>
      <Text span inherit className="nowrap">
        {ago(ts, now)}
      </Text>
    </Tooltip>
  );
}

const RANGE_LABELS: Record<RangeName, string> = {
  '1h': '1 h',
  '6h': '6 h',
  '24h': '24 h',
  '7d': '7 d',
  '30d': '30 d',
  '90d': '90 d',
  '365d': '1 y',
};

export function RangeSelect({ value, onChange, ranges }: { value: RangeName; onChange: (v: RangeName) => void; ranges: RangeName[] }) {
  return (
    <SegmentedControl size="xs" data={ranges.map((r) => ({ value: r, label: RANGE_LABELS[r] }))} value={value} onChange={(v) => onChange(v as RangeName)} />
  );
}

export function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section>
      <Title order={4} c="dimmed" tt="uppercase" fz="sm" mb={hint ? 2 : 'xs'}>
        {title}
      </Title>
      {hint && (
        <Text size="xs" c="dimmed" mb="xs">
          {hint}
        </Text>
      )}
      {children}
    </section>
  );
}
