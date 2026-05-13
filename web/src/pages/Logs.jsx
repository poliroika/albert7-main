import React, { useState, useEffect, useCallback } from 'react';
import { useWorkspace } from '../context/WorkspaceContext';
import { listLogs } from '../lib/api';
import { ScrollArea } from '../components/ui/scroll-area';
import { Badge } from '../components/ui/badge';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Search, RefreshCw, Loader2, FileText } from 'lucide-react';

const severityConfig = {
  info: { class: 'bg-sky-500/10 text-sky-400 border-sky-500/20', dotClass: 'bg-sky-500' },
  warn: { class: 'bg-amber-500/10 text-amber-400 border-amber-500/20', dotClass: 'bg-amber-500' },
  error: { class: 'bg-rose-500/10 text-rose-400 border-rose-500/20', dotClass: 'bg-rose-500' },
  debug: { class: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/20', dotClass: 'bg-zinc-500' },
};

const filterTabs = [
  { value: 'all', label: 'All' },
  { value: 'info', label: 'INFO' },
  { value: 'warn', label: 'WARN' },
  { value: 'error', label: 'ERROR' },
];

export default function Logs() {
  const { activeWorkspace, loading: workspacesLoading } = useWorkspace();
  const [logs, setLogs] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [severityFilter, setSeverityFilter] = useState('all');

  const fetchLogs = useCallback(async () => {
    if (workspacesLoading) {
      return;
    }
    if (!activeWorkspace) {
      setLogs([]);
      setTotal(0);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const params = { workspace_id: activeWorkspace.id, limit: 200 };
      if (severityFilter !== 'all') params.severity = severityFilter;
      if (searchQuery) params.q = searchQuery;
      const data = await listLogs(params);
      setLogs(data?.logs ?? []);
      setTotal(data?.total ?? 0);
    } catch (err) {
      console.error(err);
      setLogs([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [activeWorkspace, severityFilter, searchQuery, workspacesLoading]);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  const handleSearch = (e) => {
    e.preventDefault();
    fetchLogs();
  };

  return (
    <div className="flex flex-col h-[calc(100vh-56px)]">
      {/* Sticky filter bar */}
      <div className="sticky top-0 z-10 px-6 lg:px-8 py-3 border-b border-border bg-background/80 backdrop-blur">
        <div className="flex flex-wrap items-center gap-4">
          {/* Level filter tabs */}
          <div className="flex items-center rounded-lg border border-border bg-secondary/30 p-0.5" data-testid="logs-filter">
            {filterTabs.map(tab => (
              <button
                key={tab.value}
                onClick={() => setSeverityFilter(tab.value)}
                className={`px-3 py-1.5 rounded-md text-[11px] font-medium transition-all duration-160 ${
                  severityFilter === tab.value
                    ? 'bg-accent text-foreground shadow-sm'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Search */}
          <form onSubmit={handleSearch} className="flex-1 min-w-[200px] max-w-sm relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
            <Input
              data-testid="logs-search-input"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search logs..."
              className="pl-9 h-8 text-xs bg-secondary/30"
            />
          </form>

          <Button variant="ghost" size="icon" className="h-8 w-8 text-muted-foreground" onClick={fetchLogs}>
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
          <span className="text-[11px] text-muted-foreground tabular-nums">{total} logs</span>
        </div>
      </div>

      {/* Log viewer */}
      <ScrollArea className="flex-1">
        {loading || workspacesLoading ? (
          <div className="flex items-center justify-center h-40">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        ) : !activeWorkspace ? (
          <div className="flex flex-col items-center justify-center h-60 text-center px-6">
            <FileText className="h-8 w-8 text-muted-foreground mb-3" />
            <p className="text-sm text-muted-foreground">Выберите workspace в шапке или создайте его в разделе Workspaces.</p>
          </div>
        ) : logs.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-60 text-center">
            <FileText className="h-8 w-8 text-muted-foreground mb-3" />
            <p className="text-sm text-muted-foreground">No logs found</p>
          </div>
        ) : (
          <div className="font-mono text-[12px]">
            {logs.map((log, i) => {
              const config = severityConfig[log.severity] || severityConfig.info;
              return (
                <div
                  data-testid="logs-row"
                  key={log.id || i}
                  className="grid grid-cols-[140px_64px_minmax(0,1fr)] gap-4 items-start px-6 lg:px-8 py-2 hover:bg-accent/20 transition-colors duration-100 border-b border-border/20"
                >
                  <span className="text-muted-foreground/50 tabular-nums whitespace-nowrap">
                    {formatTimestamp(log.timestamp || log.created_at)}
                  </span>
                  <Badge
                    variant="outline"
                    className={`text-[9px] px-1.5 py-0 justify-center uppercase font-bold tracking-wider w-fit ${config.class}`}
                  >
                    {log.severity}
                  </Badge>
                  <span className="text-foreground/80 whitespace-pre-wrap break-all leading-relaxed">
                    {log.message}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}

function formatTimestamp(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts);
    return d.toLocaleString([], {
      month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
  } catch {
    return ts;
  }
}
