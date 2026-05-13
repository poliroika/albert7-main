import React, { useEffect, useState, useCallback } from 'react';
import { Card, CardContent } from '../components/ui/card';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import { Badge } from '../components/ui/badge';
import { ScrollArea } from '../components/ui/scroll-area';
import {
  Plug,
  Plus,
  Search,
  Power,
  PowerOff,
  Trash2,
  ExternalLink,
  Loader2,
  AlertTriangle,
} from 'lucide-react';
import { motion } from 'framer-motion';
import { toast } from 'sonner';
import {
  listMcpServers,
  addMcpServer,
  updateMcpServer,
  deleteMcpServer,
  discoverMcpServers,
} from '../lib/api';

const transports = ['stdio', 'http', 'sse'];

export default function MCPRegistry() {
  const [servers, setServers] = useState([]);
  const [loading, setLoading] = useState(false);
  const [discoveryQuery, setDiscoveryQuery] = useState('');
  const [discoveryResults, setDiscoveryResults] = useState([]);
  const [discoveryLoading, setDiscoveryLoading] = useState(false);
  const [newServer, setNewServer] = useState({
    name: '',
    transport: 'stdio',
    command: '',
    url: '',
    description: '',
  });
  const [creating, setCreating] = useState(false);

  const fetchServers = useCallback(async () => {
    setLoading(true);
    try {
      const list = await listMcpServers();
      setServers(Array.isArray(list) ? list : []);
    } catch (err) {
      console.error(err);
      toast.error('Не удалось загрузить MCP серверы');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchServers();
  }, [fetchServers]);

  const handleCreate = async () => {
    if (!newServer.name.trim()) {
      toast.error('Имя обязательно');
      return;
    }
    if (newServer.transport === 'stdio' && !newServer.command.trim()) {
      toast.error('Stdio требует command');
      return;
    }
    if (newServer.transport !== 'stdio' && !newServer.url.trim()) {
      toast.error('HTTP/SSE требует URL');
      return;
    }
    setCreating(true);
    try {
      await addMcpServer({ ...newServer, source: 'user' });
      toast.success('MCP сервер добавлен (disabled — включите вручную)');
      setNewServer({ name: '', transport: 'stdio', command: '', url: '', description: '' });
      fetchServers();
    } catch (err) {
      toast.error(`Не удалось добавить: ${err?.response?.data?.reason || err?.message}`);
    } finally {
      setCreating(false);
    }
  };

  const handleToggle = async (server) => {
    const next = server.status === 'enabled' ? 'disabled' : 'enabled';
    if (next === 'enabled' && !window.confirm(
      `Включить MCP сервер «${server.name}»? Он сможет выполнять команды от имени агента.`
    )) {
      return;
    }
    try {
      await updateMcpServer(server.id, { status: next });
      toast.success(next === 'enabled' ? 'Включено' : 'Выключено');
      fetchServers();
    } catch (err) {
      toast.error(`Ошибка: ${err?.response?.data?.reason || err?.message}`);
    }
  };

  const handleDelete = async (server) => {
    if (!window.confirm(`Удалить MCP сервер «${server.name}»?`)) return;
    try {
      await deleteMcpServer(server.id);
      toast.success('Удалено');
      fetchServers();
    } catch (err) {
      toast.error(`Ошибка удаления: ${err?.response?.data?.reason || err?.message}`);
    }
  };

  const handleDiscover = async () => {
    if (!discoveryQuery.trim()) return;
    setDiscoveryLoading(true);
    try {
      const result = await discoverMcpServers({
        query: discoveryQuery.trim(),
        max_results: 8,
      });
      if (result?.ok) {
        setDiscoveryResults(result.results || []);
      } else {
        toast.error(`Discovery failed: ${result?.reason || 'unknown'}`);
      }
    } catch (err) {
      toast.error(`Discovery failed: ${err?.message}`);
    } finally {
      setDiscoveryLoading(false);
    }
  };

  const handleInstall = async (item) => {
    if (!window.confirm(
      `Зарегистрировать «${item.name}» как новый MCP сервер? ` +
        `Он будет создан в статусе DISABLED — вы сможете включить его вручную.`
    )) {
      return;
    }
    try {
      await addMcpServer({
        name: item.name,
        transport: 'stdio',
        command: item.install_hint_npx || '',
        description: item.description,
        install_notes: item.url,
        source: 'discovered',
        status: 'disabled',
      });
      toast.success(`Зарегистрировано: ${item.name}`);
      fetchServers();
    } catch (err) {
      toast.error(`Не удалось зарегистрировать: ${err?.response?.data?.reason || err?.message}`);
    }
  };

  return (
    <div className="px-4 lg:px-8 py-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-display font-semibold tracking-tight flex items-center gap-2">
            <Plug className="h-5 w-5" /> MCP Registry
          </h2>
          <p className="text-sm text-muted-foreground">
            Подключённые MCP серверы. Включённые сервера автоматически добавляют свои tools в ouroboros (префикс `mcp_`).
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={fetchServers} disabled={loading}>
          {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : 'Refresh'}
        </Button>
      </div>

      <Card className="bg-card/60 border-border/70">
        <CardContent className="p-4 space-y-3">
          <div className="flex items-center gap-2">
            <Plus className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-semibold">Add server manually</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <Input
              placeholder="Name (e.g. memory-bank)"
              value={newServer.name}
              onChange={(e) => setNewServer({ ...newServer, name: e.target.value })}
            />
            <select
              className="h-9 rounded-md border border-border bg-background px-3 text-sm"
              value={newServer.transport}
              onChange={(e) => setNewServer({ ...newServer, transport: e.target.value })}
            >
              {transports.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
            {newServer.transport === 'stdio' ? (
              <Input
                placeholder="Command (e.g. npx -y @modelcontextprotocol/server-memory)"
                className="md:col-span-2"
                value={newServer.command}
                onChange={(e) => setNewServer({ ...newServer, command: e.target.value })}
              />
            ) : (
              <Input
                placeholder="URL (https://example.com/mcp)"
                className="md:col-span-2"
                value={newServer.url}
                onChange={(e) => setNewServer({ ...newServer, url: e.target.value })}
              />
            )}
            <Input
              placeholder="Description (optional)"
              className="md:col-span-2"
              value={newServer.description}
              onChange={(e) => setNewServer({ ...newServer, description: e.target.value })}
            />
          </div>
          <div className="flex justify-end">
            <Button size="sm" onClick={handleCreate} disabled={creating} className="gap-2">
              <Plus className="h-3.5 w-3.5" /> Add (disabled)
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card className="bg-card/60 border-border/70">
        <CardContent className="p-4 space-y-3">
          <div className="flex items-center gap-2">
            <Search className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-semibold">Discover MCP servers on GitHub</span>
          </div>
          <div className="flex gap-2">
            <Input
              placeholder="e.g. postgres, slack, filesystem..."
              value={discoveryQuery}
              onChange={(e) => setDiscoveryQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleDiscover()}
            />
            <Button onClick={handleDiscover} size="sm" disabled={discoveryLoading} className="gap-2">
              {discoveryLoading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />}
              Search
            </Button>
          </div>
          {discoveryResults.length > 0 && (
            <ScrollArea className="max-h-[300px]">
              <div className="space-y-2">
                {discoveryResults.map((item, idx) => (
                  <div
                    key={`${item.name}-${idx}`}
                    className="rounded-lg border border-border/50 bg-secondary/20 p-3"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-sm font-semibold truncate">{item.name}</span>
                          {item.license && (
                            <Badge variant="outline" className="text-[10px]">
                              {item.license}
                            </Badge>
                          )}
                          <span className="text-[10px] text-muted-foreground">
                            ★ {item.stars}
                          </span>
                        </div>
                        <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                          {item.description || '(no description)'}
                        </p>
                        {item.install_hint_npx && (
                          <p className="mt-1 font-mono text-[10px] text-muted-foreground/80">
                            hint: {item.install_hint_npx}
                          </p>
                        )}
                      </div>
                      <div className="flex flex-col gap-1 shrink-0">
                        <a
                          href={item.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-xs text-blue-400 hover:underline flex items-center gap-1"
                        >
                          <ExternalLink className="h-3 w-3" /> Open
                        </a>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => handleInstall(item)}
                        >
                          Install (disabled)
                        </Button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </ScrollArea>
          )}
        </CardContent>
      </Card>

      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold">Registered servers</h3>
          <span className="text-xs text-muted-foreground">{servers.length} total</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {servers.map((server, i) => (
            <motion.div
              key={server.id}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.18, delay: i * 0.03 }}
            >
              <Card className="bg-card/60 border-border/70">
                <CardContent className="p-4 space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-sm font-semibold truncate">{server.name}</p>
                      <p className="text-[11px] text-muted-foreground font-mono truncate">
                        {server.transport} · {server.source}
                      </p>
                    </div>
                    <Badge
                      variant="outline"
                      className={
                        server.status === 'enabled'
                          ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                          : 'bg-zinc-500/10 text-zinc-400 border-zinc-500/30'
                      }
                    >
                      {server.status}
                    </Badge>
                  </div>
                  {server.description && (
                    <p className="text-[11px] text-muted-foreground line-clamp-2">{server.description}</p>
                  )}
                  {server.transport === 'stdio' && server.command && (
                    <p className="text-[10px] font-mono text-muted-foreground/80 truncate">
                      ${server.command} {(server.args || []).join(' ')}
                    </p>
                  )}
                  {server.url && (
                    <p className="text-[10px] font-mono text-muted-foreground/80 truncate">{server.url}</p>
                  )}
                  {server.status === 'enabled' && (
                    <div className="flex items-center gap-1.5 text-[10px] text-amber-400">
                      <AlertTriangle className="h-3 w-3" /> tools зарегистрированы под `mcp_{server.name}__*`
                    </div>
                  )}
                  <div className="flex justify-end gap-2 pt-1">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => handleToggle(server)}
                      className="gap-1.5"
                    >
                      {server.status === 'enabled' ? (
                        <>
                          <PowerOff className="h-3 w-3" /> Disable
                        </>
                      ) : (
                        <>
                          <Power className="h-3 w-3" /> Enable
                        </>
                      )}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => handleDelete(server)}
                      className="gap-1.5 border-destructive/30 text-destructive hover:bg-destructive/10"
                    >
                      <Trash2 className="h-3 w-3" /> Delete
                    </Button>
                  </div>
                </CardContent>
              </Card>
            </motion.div>
          ))}
          {servers.length === 0 && !loading && (
            <div className="col-span-full flex flex-col items-center justify-center h-32 text-muted-foreground">
              <Plug className="h-6 w-6 mb-2" />
              <p className="text-sm">Пока ни одного MCP сервера. Добавьте вручную или поищите на GitHub.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
