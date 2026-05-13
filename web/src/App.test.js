import React, { useEffect } from 'react';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { ThemeProvider, useTheme } from './context/ThemeContext';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

function Probe() {
  const { theme } = useTheme();

  useEffect(() => {
    document.body.setAttribute('data-theme-probe', theme);
  }, [theme]);

  return <span>{theme}</span>;
}

it('applies the imported default theme', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);

  await act(async () => {
    root.render(
      <ThemeProvider>
        <Probe />
      </ThemeProvider>,
    );
  });

  expect(container.textContent).toContain('dark');
  expect(document.documentElement.classList.contains('dark')).toBe(true);
  expect(document.body.getAttribute('data-theme-probe')).toBe('dark');

  await act(async () => {
    root.unmount();
  });
  container.remove();
});
