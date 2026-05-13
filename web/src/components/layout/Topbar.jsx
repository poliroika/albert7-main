import React from 'react';
import { useLocation } from 'react-router-dom';
import { useTheme } from '../../context/ThemeContext';
import { useWorkspace } from '../../context/WorkspaceContext';
import { Button } from '../ui/button';
import { Sun, Moon, Search, PanelLeft } from 'lucide-react';

const pageTitles = {
  '/chat': 'Chat Workspace',
  '/memory': 'Memory Graph',
  '/runs': 'Runs',
  '/logs': 'Logs',
  '/dashboard': 'Dashboard',
  '/workspaces': 'Workspaces',
  '/settings': 'Settings',
};

export default function Topbar({ sidebarCollapsed, onToggleSidebar }) {
  const location = useLocation();
  const { theme, toggleTheme } = useTheme();
  const { serverLlmModel } = useWorkspace();

  const getTitle = () => {
    for (const [path, title] of Object.entries(pageTitles)) {
      if (location.pathname.startsWith(path)) return title;
    }
    return 'Umbrella';
  };

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-border/70 bg-background/70 backdrop-blur supports-[backdrop-filter]:bg-background/50 px-4">
      <div className="flex items-center gap-3">
        {sidebarCollapsed && (
          <Button variant="ghost" size="icon" className="h-8 w-8" onClick={onToggleSidebar}>
            <PanelLeft className="h-4 w-4" />
          </Button>
        )}
        <div className="flex min-w-0 flex-col gap-0.5 sm:flex-row sm:items-baseline sm:gap-3">
          <h1 className="text-sm font-semibold tracking-tight" style={{ fontFamily: 'var(--font-display)' }}>
            {getTitle()}
          </h1>
          {serverLlmModel ? (
            <span
              className="truncate font-mono text-[11px] text-muted-foreground"
              title={`Модель из .env (OUROBOROS_MODEL / LLM_MODEL): ${serverLlmModel}`}
            >
              {serverLlmModel}
            </span>
          ) : null}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          className="hidden sm:flex items-center gap-2 text-xs text-muted-foreground h-8 px-3"
          data-testid="global-search"
        >
          <Search className="h-3.5 w-3.5" />
          <span>Search...</span>
          <kbd className="ml-2 pointer-events-none inline-flex h-5 select-none items-center gap-1 rounded border bg-muted px-1.5 font-mono text-[10px] font-medium text-muted-foreground">
            <span className="text-xs">⌘</span>K
          </kbd>
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          onClick={toggleTheme}
          data-testid="theme-toggle"
        >
          {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </Button>
      </div>
    </header>
  );
}
