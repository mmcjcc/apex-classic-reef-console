import { createTheme, type MantineColorsTuple } from '@mantine/core';

// A dark scale of blue-greys (shared with the homelab console it was first built beside).
const dark: MantineColorsTuple = [
  '#c9d3e0',
  '#aab6c7',
  '#8794a8',
  '#5d6b80',
  '#3a4658',
  '#2a3444',
  '#1c2431',
  '#141b26',
  '#0e141d',
  '#090d14',
];

export const theme = createTheme({
  primaryColor: 'cyan',
  primaryShade: { light: 7, dark: 5 },
  defaultRadius: 'md',
  colors: { dark },
  fontFamily: 'Inter, "Segoe UI", system-ui, -apple-system, sans-serif',
  fontFamilyMonospace: 'ui-monospace, "Cascadia Code", "SF Mono", Menlo, Consolas, monospace',
  headings: { fontWeight: '650' },
  components: {
    Table: { defaultProps: { verticalSpacing: 6, horizontalSpacing: 'sm', highlightOnHover: true } },
    Card: { defaultProps: { shadow: 'none', withBorder: true } },
    Badge: { defaultProps: { radius: 'sm' } },
  },
});
