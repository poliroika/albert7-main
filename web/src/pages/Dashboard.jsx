import React, { useState, useEffect } from 'react';
import { useWorkspace } from '../context/WorkspaceContext';
import { getDashboardStats } from '../lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card';
import { Badge } from '../components/ui/badge';
import {
  Table, TableBody, TableCell, TableHead, TableHeader, TableRow
} from '../components/ui/table';
import { useNavigate } from 'react-router-dom';
import { Play, DollarSign, Loader2, BarChart3, TrendingUp, Activity } from 'lucide-react';
import { motion } from 'framer-motion';
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';

const statusConfig = {
  queued: { class: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/20' },
  running: { class: 'bg-blue-500/10 text-blue-400 border-blue-500/20' },
  completed: { class: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' },
  failed: { class: 'bg-rose-500/10 text-rose-400 border-rose-500/20' },
  cancelled: { class: 'bg-amber-500/10 text-amber-400 border-amber-500/20' },
};

const DAY_MS = 24 * 60 * 60 * 1000;

// Build chart data from real runs only. No synthetic cost/runs.
function generateChartData(recentRuns) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const buckets = new Map();
  for (let i = 6; i >= 0; i -= 1) {
    const date = new Date(today.getTime() - i * DAY_MS);
    const key = date.toISOString().slice(0, 10);
    buckets.set(key, {
      day: date.toLocaleDateString(undefined, { weekday: 'short' }),
      runs: 0,
      cost: 0,
    });
  }

  (recentRuns || []).forEach((run) => {
    const date = new Date(run.created_at || run.updated_at || 0);
    if (Number.isNaN(date.getTime())) return;
    date.setHours(0, 0, 0, 0);
    const key = date.toISOString().slice(0, 10);
    const bucket = buckets.get(key);
    if (!bucket) return;
    bucket.runs += 1;
    bucket.cost += Number(run.total_cost || 0);
  });

  return Array.from(buckets.values()).map((bucket) => ({
    ...bucket,
    cost: Number(bucket.cost.toFixed(4)),
  }));
}

const CustomTooltip = ({ active, payload, label }) => {
  if (active && payload && payload.length) {
    return (
      <div className="rounded-lg border border-border bg-card px-3 py-2 shadow-lg">
        <p className="text-[10px] text-muted-foreground mb-1">{label}</p>
        {payload.map((entry, i) => (
          <p key={i} className="text-xs font-medium" style={{ color: entry.color }}>
            {entry.name}: {entry.dataKey === 'cost' ? `$${Number(entry.value || 0).toFixed(4)}` : entry.value}
          </p>
        ))}
      </div>
    );
  }
  return null;
};

export default function Dashboard() {
  const { activeWorkspace } = useWorkspace();
  const navigate = useNavigate();
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!activeWorkspace) return;
    setLoading(true);
    getDashboardStats(activeWorkspace.id)
      .then(setStats)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [activeWorkspace]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[calc(100vh-56px)]">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!stats) {
    return (
      <div className="flex flex-col items-center justify-center h-[calc(100vh-56px)]">
        <BarChart3 className="h-8 w-8 text-muted-foreground mb-3" />
        <p className="text-sm text-muted-foreground">Select a workspace to view dashboard</p>
      </div>
    );
  }

  const chartData = generateChartData(stats.recent_runs || []);
  const hasRecordedCost = chartData.some((point) => point.cost > 0);

  const statCards = [
    { label: 'Total Runs', value: stats.total_runs, icon: Play, color: 'text-blue-400', bgColor: 'bg-blue-500/8' },
    { label: 'Success Rate', value: `${stats.success_rate}%`, icon: TrendingUp, color: 'text-emerald-400', bgColor: 'bg-emerald-500/8' },
    { label: 'Total Cost', value: `$${stats.total_cost.toFixed(2)}`, icon: DollarSign, color: 'text-amber-400', bgColor: 'bg-amber-500/8' },
    { label: 'Active Runs', value: stats.active_runs, icon: Activity, color: 'text-blue-400', bgColor: 'bg-blue-500/8' },
  ];

  return (
    <div className="px-6 lg:px-8 py-6">
      <div className="mb-6">
        <h2 className="text-lg font-semibold tracking-tight">Dashboard</h2>
        <p className="text-xs text-muted-foreground mt-0.5">Overview of your workspace activity</p>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {statCards.map((stat, i) => (
          <motion.div
            key={i}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, delay: i * 0.04 }}
          >
            <Card className="bg-card border-border">
              <CardContent className="p-4">
                <div className={`flex h-8 w-8 items-center justify-center rounded-lg ${stat.bgColor} mb-3`}>
                  <stat.icon className={`h-4 w-4 ${stat.color}`} />
                </div>
                <p className="text-2xl font-semibold tabular-nums tracking-tight">{stat.value}</p>
                <p className="text-[11px] text-muted-foreground mt-0.5">{stat.label}</p>
              </CardContent>
            </Card>
          </motion.div>
        ))}
      </div>

      {/* Charts row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
        {/* Runs per day */}
        <Card className="bg-card border-border" data-testid="dashboard-runs-chart">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Runs Per Day</CardTitle>
          </CardHeader>
          <CardContent className="pb-4">
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: -20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsl(220 14% 14%)" vertical={false} />
                <XAxis dataKey="day" tick={{ fontSize: 10, fill: 'hsl(215 12% 58%)' }} axisLine={false} tickLine={false} />
                <YAxis tick={{ fontSize: 10, fill: 'hsl(215 12% 58%)' }} axisLine={false} tickLine={false} />
                <Tooltip content={<CustomTooltip />} />
                <Bar dataKey="runs" fill="#3b82f6" radius={[4, 4, 0, 0]} maxBarSize={32} name="Runs" />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>

        {/* Cost over time */}
        <Card className="bg-card border-border" data-testid="dashboard-cost-chart">
          <CardHeader className="pb-2">
            <CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Cost Over Time</CardTitle>
          </CardHeader>
          <CardContent className="pb-4">
            {hasRecordedCost ? (
              <ResponsiveContainer width="100%" height={180}>
                <AreaChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: -20 }}>
                  <defs>
                    <linearGradient id="costGradient" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#f59e0b" stopOpacity={0.2} />
                      <stop offset="95%" stopColor="#f59e0b" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="hsl(220 14% 14%)" vertical={false} />
                  <XAxis dataKey="day" tick={{ fontSize: 10, fill: 'hsl(215 12% 58%)' }} axisLine={false} tickLine={false} />
                  <YAxis tick={{ fontSize: 10, fill: 'hsl(215 12% 58%)' }} axisLine={false} tickLine={false} />
                  <Tooltip content={<CustomTooltip />} />
                  <Area type="monotone" dataKey="cost" stroke="#f59e0b" fill="url(#costGradient)" strokeWidth={2} name="Cost ($)" />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div
                data-testid="dashboard-cost-zero-state"
                className="flex h-[180px] flex-col items-center justify-center rounded-md border border-dashed border-border/70 text-center"
              >
                <p className="text-2xl font-semibold tabular-nums text-foreground">$0.0000</p>
                <p className="mt-1 text-xs text-muted-foreground">No recorded cost in recent runs</p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Recent runs */}
      <Card className="bg-card border-border">
        <CardHeader className="pb-3">
          <CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Recent Runs</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {stats.recent_runs?.length > 0 ? (
            <Table>
              <TableHeader>
                <TableRow className="hover:bg-transparent">
                  <TableHead className="text-[10px] uppercase tracking-wider">Status</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wider">Model</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wider">Steps</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wider">Cost</TableHead>
                  <TableHead className="text-[10px] uppercase tracking-wider">Created</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {stats.recent_runs.map(run => {
                  const sc = statusConfig[run.status] || statusConfig.queued;
                  return (
                    <TableRow
                      key={run.id}
                      className="cursor-pointer hover:bg-accent/30 transition-colors duration-160"
                      onClick={() => navigate(`/runs/${run.id}`)}
                    >
                      <TableCell>
                        <Badge variant="outline" className={`text-[10px] ${sc.class}`}>{run.status}</Badge>
                      </TableCell>
                      <TableCell className="text-xs font-mono text-muted-foreground">{run.model || 'N/A'}</TableCell>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">{run.total_steps || 0}</TableCell>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">${(run.total_cost || 0).toFixed(4)}</TableCell>
                      <TableCell className="text-[11px] text-muted-foreground/60">{new Date(run.created_at).toLocaleString()}</TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          ) : (
            <div className="flex items-center justify-center h-32">
              <p className="text-sm text-muted-foreground">No recent runs</p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
