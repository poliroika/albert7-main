import React, { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useWorkspace } from '../context/WorkspaceContext';
import { listRuns, getRun, getRunSteps, getRunTimeline, deleteRun, cancelRun } from '../lib/api';
import { ScrollArea } from '../components/ui/scroll-area';
import { Badge } from '../components/ui/badge';
import { Button } from '../components/ui/button';
import { Card } from '../components/ui/card';
import HarnessProgressPanel from '../components/chat/HarnessProgressPanel';
import {
  ArrowLeft, CheckCircle2, XCircle, AlertCircle, Clock, Loader2,
  Play, Search, Code, Database, FileText, Globe, Brain, MessageSquare, Zap,
  Terminal, ChevronDown, ChevronRight, DollarSign, Trash2, Square
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';
import { toast } from 'sonner';

const statusConfig = {
  queued: { icon: Clock, label: 'Queued', class: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/20', dot: 'bg-zinc-500' },
  running: { icon: Loader2, label: 'Running', class: 'bg-blue-500/10 text-blue-400 border-blue-500/20', dot: 'bg-blue-500 animate-pulse-dot' },
  completed: { icon: CheckCircle2, label: 'Completed', class: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20', dot: 'bg-emerald-500' },
  failed: { icon: XCircle, label: 'Failed', class: 'bg-rose-500/10 text-rose-400 border-rose-500/20', dot: 'bg-rose-500' },
  cancelled: { icon: AlertCircle, label: 'Cancelled', class: 'bg-amber-500/10 text-amber-400 border-amber-500/20', dot: 'bg-amber-500' },
};

const toolIcons = {
  web_search: Search, python: Code, db_query: Database,
  file_read: FileText, api_call: Globe,
};

const stepIcons = {
  thinking: Brain, tool_call: Terminal, response: MessageSquare,
};

function PhaseTimeline({ timeline }) {
  if (!timeline || !Array.isArray(timeline.phases) || timeline.phases.length === 0) {
    return null;
  }
  const fmtDuration = (ms) => {
    if (!ms) return '0s';
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const rem = s % 60;
    if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  };
  const verifyClass = (s) => {
    if (s === 'passed') return 'text-emerald-400';
    if (s === 'failed') return 'text-rose-400';
    if (s === 'skipped') return 'text-amber-400';
    return 'text-muted-foreground';
  };
  return (
    <div className="rounded-md border border-border/50 bg-secondary/20 p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Phase Timeline
        </span>
        <span className="text-[10px] text-muted-foreground/70">
          {timeline.phases.length} phase{timeline.phases.length === 1 ? '' : 's'}
        </span>
      </div>
      <div className="space-y-1.5">
        {timeline.phases.map((phase, idx) => (
          <div
            key={`${phase.name}-${idx}`}
            className="grid grid-cols-[140px_1fr_60px_60px_60px_70px_70px] gap-2 items-center text-[11px] py-1 border-b border-border/30 last:border-b-0"
          >
            <span className="font-medium truncate">{phase.label || phase.name}</span>
            <span className="text-muted-foreground tabular-nums font-mono">
              {fmtDuration(phase.duration_ms)}
            </span>
            <span className="tabular-nums text-right" title="rounds">
              {phase.rounds || 0}r
            </span>
            <span className="tabular-nums text-right" title="tool calls">
              {phase.tool_calls || 0}t
            </span>
            <span className="tabular-nums text-right text-emerald-400/80" title="write tool calls">
              {phase.write_tool_calls || 0}w
            </span>
            <span
              className="tabular-nums text-right text-rose-400/80"
              title="preflight errors"
            >
              {phase.preflight_errors ? `!${phase.preflight_errors}` : '-'}
            </span>
            <span
              className={`text-right text-[10px] uppercase font-semibold ${verifyClass(phase.verification_status)}`}
              title="verification status"
            >
              {phase.verification_status || ''}
            </span>
          </div>
        ))}
      </div>
      <div className="mt-2 text-[10px] text-muted-foreground/70">
        legend: <span className="font-mono">r</span>=rounds,
        <span className="font-mono"> t</span>=tool calls,
        <span className="font-mono text-emerald-400/80"> w</span>=writes,
        <span className="font-mono text-rose-400/80"> !</span>=preflight errors
      </div>
    </div>
  );
}

function ExpandableRunRow({ run, onDeleted, onStopped }) {
  const navigate = useNavigate();
  const [expanded, setExpanded] = useState(false);
  const [steps, setSteps] = useState([]);
  const [details, setDetails] = useState(run);
  const [timeline, setTimeline] = useState(null);
  const [loadingSteps, setLoadingSteps] = useState(false);

  const status = statusConfig[run.status] || statusConfig.queued;
  const canStop = run.status === 'running' || run.status === 'queued';

  const handleExpand = async () => {
    if (!expanded && steps.length === 0) {
      setLoadingSteps(true);
      try {
        const [runData, stepData, timelineData] = await Promise.all([
          getRun(run.id),
          getRunSteps(run.id),
          getRunTimeline(run.id).catch(() => ({ phases: [] })),
        ]);
        setDetails(runData || run);
        setSteps(stepData);
        setTimeline(timelineData);
      } catch (e) {
        console.error(e);
      } finally {
        setLoadingSteps(false);
      }
    }
    setExpanded(!expanded);
  };

  const fullResult = details?.full_result && typeof details.full_result === 'object' ? details.full_result : {};
  const isHarness = details?.mode === 'harness' || run?.mode === 'harness' || !!details?.harness_meta || !!fullResult?.harness_result;
  const verification = fullResult.verification_report && typeof fullResult.verification_report === 'object'
    ? fullResult.verification_report
    : null;
  const changedFiles = fullResult.changes_made || [];
  const handleDelete = async (event) => {
    event.stopPropagation();
    const ok = window.confirm(`Delete run ${run.id} and its logs/memory artifacts?`);
    if (!ok) return;
    const wsId = run.workspace_id || details?.workspace_id;
    try {
      const result = await deleteRun(run.id, wsId ? { workspace_id: wsId } : undefined);
      const removedCount = result?.report?.removed_count ?? 0;
      const detached = result?.detached;
      const errors = Array.isArray(result?.report?.errors) ? result.report.errors : [];
      const kept = Array.isArray(result?.report?.kept_paths) ? result.report.kept_paths : [];
      if (detached) {
        toast.success('Run остановлен и оставлен в Runs пока worker завершается');
      } else if (errors.length > 0) {
        const head = errors.slice(0, 2).join('; ');
        const more = errors.length > 2 ? ` (+${errors.length - 2} ещё)` : '';
        toast.warning(
          `Run удалён, но ${errors.length} артефакт(ов) не удалось стереть: ${head}${more}`,
          { duration: 8000 },
        );
      } else if (kept.length > 0 && removedCount === 0) {
        toast.warning(
          `Удалено 0 артефактов; ${kept.length} путей пропущено (см. report).`,
          { duration: 6000 },
        );
      } else if (removedCount > 0) {
        toast.success(`Run удалён, очищено ${removedCount} артефактов`);
      } else {
        toast.success('Run удалён');
      }
      onDeleted?.(run.id);
    } catch (err) {
      console.error(err);
      const detail = err?.response?.data?.reason || err?.message || 'Неизвестная ошибка';
      toast.error(`Не удалось удалить run: ${detail}`);
    }
  };
  const handleStop = async (event) => {
    event.stopPropagation();
    onStopped?.(run.id, { optimistic: 'cancelling' });
    try {
      const result = await cancelRun(run.id, { wait: 15, force_after: 5 });
      const stopMethod = result?.stop_method;
      const finalStatus = result?.status || 'cancelled';
      if (stopMethod === 'forced_detach') {
        toast.success('Run остановлен (worker отсоединён, дойдёт в фоне)');
      } else {
        toast.success(finalStatus === 'cancelled' ? 'Run отменён' : 'Stop запрошен');
      }
      onStopped?.(run.id, { status: finalStatus });
    } catch (err) {
      console.error(err);
      toast.error('Failed to stop run');
      onStopped?.(run.id, { revert: true });
    }
  };
  const handleOpenChat = (event) => {
    event.stopPropagation();
    const threadId = details?.thread_id || run.thread_id;
    if (threadId) navigate(`/chat/${threadId}`);
  };

  return (
    <div data-testid="runs-table-row" className="border-b border-border/50 last:border-b-0">
      {/* Row */}
      <div
        className="grid grid-cols-[32px_120px_120px_60px_80px_80px_1fr_160px] gap-3 items-center px-4 py-3 hover:bg-accent/30 cursor-pointer transition-colors duration-160"
        onClick={handleExpand}
      >
        <motion.div animate={{ rotate: expanded ? 90 : 0 }} transition={{ duration: 0.12 }}>
          <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />
        </motion.div>
        <Badge variant="outline" className={`text-[10px] w-fit ${status.class}`}>
          <div className={`w-1.5 h-1.5 rounded-full mr-1.5 ${status.dot}`} />
          {status.label}
        </Badge>
        <span className="text-xs font-mono text-muted-foreground">
          {run.model
            || run.active_model
            || (Array.isArray(run.harness_meta?.models) ? run.harness_meta.models[0] : null)
            || 'N/A'}
        </span>
        <span className="text-xs tabular-nums text-muted-foreground">{run.total_steps || 0}</span>
        <span className="text-xs tabular-nums text-muted-foreground">${(run.total_cost || 0).toFixed(4)}</span>
        <span className="text-xs tabular-nums text-muted-foreground">{run.total_duration_ms ? `${run.total_duration_ms}ms` : '-'}</span>
        <span className="text-[11px] text-muted-foreground/60 text-right">{new Date(run.created_at).toLocaleString()}</span>
        <div className="flex items-center justify-end gap-2">
          {canStop && (
            <Button
              variant="outline"
              size="sm"
              className="h-7 gap-1.5 px-2 text-[11px] border-amber-500/30 text-amber-400 hover:bg-amber-500/10"
              onClick={handleStop}
              data-testid="stop-run-button"
              title="Stop this run"
            >
              <Square className="h-3 w-3" />
              Stop
            </Button>
          )}
          {(details?.thread_id || run.thread_id) && (
            <Button
              variant="outline"
              size="sm"
              className="h-7 gap-1.5 px-2 text-[11px]"
              onClick={handleOpenChat}
              title="Open linked chat"
            >
              <MessageSquare className="h-3 w-3" />
              Chat
            </Button>
          )}
          <Button
            variant="outline"
            size="sm"
            className="h-7 gap-1.5 px-2 text-[11px] border-destructive/30 text-destructive hover:bg-destructive/10"
            onClick={handleDelete}
            data-testid="delete-run-button"
            title="Delete run logs and memory"
          >
            <Trash2 className="h-3 w-3" />
            Delete
          </Button>
        </div>
      </div>

      {/* Expanded details */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-4 pl-12">
              {loadingSteps ? (
                <div className="flex items-center gap-2 py-4">
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
                  <span className="text-xs text-muted-foreground">Loading steps...</span>
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1fr)_300px]">
                  {/* Timeline */}
                  <div className="space-y-3">
                    {isHarness && (
                      <div className="overflow-hidden rounded-md border border-border/50 bg-secondary/10">
                        <HarnessProgressPanel run={details} />
                      </div>
                    )}

                    <div className="rounded-md border border-border/50 bg-secondary/20 p-3">
                      <div className="flex items-center justify-between gap-3 mb-2">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Run Details</span>
                        <span className="text-[10px] font-mono text-muted-foreground">{details?.id}</span>
                      </div>
                      <p className="text-xs text-muted-foreground/90 whitespace-pre-wrap leading-relaxed">
                        {details?.result_preview || fullResult.final_message || fullResult.error || 'No result summary recorded.'}
                      </p>
                      {verification?.summary && (
                        <pre className="mt-3 max-h-48 overflow-auto rounded bg-[#0A0C10] border border-border/30 p-2 text-[10px] text-muted-foreground/90 whitespace-pre-wrap">
                          {verification.summary}
                        </pre>
                      )}
                    </div>

                    <PhaseTimeline timeline={timeline} />

                    <div className="space-y-0">
                      {steps.length === 0 ? (
                        <div className="rounded-md border border-border/50 bg-secondary/20 p-3 text-xs text-muted-foreground">
                          No step log was recorded for this run.
                        </div>
                      ) : steps.map((step, i) => {
                        const isLast = i === steps.length - 1;
                        const stepStatus = statusConfig[step.status] || statusConfig.completed;
                        const StepIcon = step.type === 'tool_call'
                          ? (toolIcons[step.name] || Terminal)
                          : (stepIcons[step.type] || Zap);

                        return (
                          <div key={step.id || i} className="relative pl-6 pb-3 last:pb-0">
                            {!isLast && <div className="absolute left-[7px] top-[14px] bottom-0 w-[2px] bg-border/50 rounded-full" />}
                            <div className={`absolute left-[3px] top-[5px] h-2.5 w-2.5 rounded-full ${stepStatus.dot}`} />
                            <div className="flex items-center justify-between">
                              <div className="flex items-center gap-2 min-w-0">
                                <StepIcon className="h-3 w-3 text-muted-foreground shrink-0" />
                                <span className="text-xs font-medium truncate">{step.type === 'tool_call' ? step.name : step.name || step.type}</span>
                              </div>
                              <span className="text-[10px] text-muted-foreground tabular-nums font-mono">
                                {step.duration_ms}ms
                              </span>
                            </div>
                            {step.data && (
                              <pre className="mt-1.5 max-h-28 overflow-auto rounded bg-[#0A0C10] border border-border/30 p-2 text-[10px] text-muted-foreground/80 whitespace-pre-wrap">
                                {step.data}
                              </pre>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  {/* Cost breakdown */}
                  <div className="rounded-lg border border-border/50 bg-secondary/20 p-3">
                    <div className="flex items-center gap-2 mb-3">
                      <DollarSign className="h-3 w-3 text-muted-foreground" />
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Cost Breakdown</span>
                    </div>
                    <div className="space-y-2">
                      {steps.filter(s => s.cost > 0).map((step, i) => (
                        <div key={i} className="flex justify-between text-[11px]">
                          <span className="text-muted-foreground">{step.name || step.type}</span>
                          <span className="tabular-nums font-mono">${step.cost.toFixed(4)}</span>
                        </div>
                      ))}
                      <div className="border-t border-border/50 pt-2 flex justify-between text-xs font-medium">
                        <span>Total</span>
                        <span className="tabular-nums font-mono">${(run.total_cost || 0).toFixed(4)}</span>
                      </div>
                    </div>
                    {run.tools_used?.length > 0 && (
                      <div className="mt-3 pt-3 border-t border-border/50">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-2">Tools Used</span>
                        <div className="flex flex-wrap gap-1.5">
                          {run.tools_used.map(tool => (
                            <Badge key={tool} variant="outline" className="text-[9px] px-1.5 py-0">{tool}</Badge>
                          ))}
                        </div>
                      </div>
                    )}
                    <div className="mt-3 pt-3 border-t border-border/50 space-y-2 text-[11px]">
                      <div className="flex justify-between gap-3">
                        <span className="text-muted-foreground">Attempts</span>
                        <span className="font-mono">{details?.attempt || 1}/{details?.max_attempts || 1}</span>
                      </div>
                      <div className="flex justify-between gap-3">
                        <span className="text-muted-foreground">Verify Remediation</span>
                        <span className="font-mono">
                          {details?.verification_remediation_attempts_used ?? 0}/{details?.verification_remediation_max ?? details?.max_verify_retries ?? 0}
                        </span>
                      </div>
                      <div className="flex justify-between gap-3">
                        <span className="text-muted-foreground">Max Rounds</span>
                        <span className="font-mono">{details?.max_rounds === 0 ? '∞' : (details?.max_rounds || '-')}</span>
                      </div>
                      {changedFiles.length > 0 && (
                        <div className="pt-2">
                          <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground block mb-2">Changed Files</span>
                          <div className="space-y-1">
                            {changedFiles.slice(0, 8).map(file => (
                              <div key={file} className="truncate font-mono text-[10px] text-muted-foreground/80">{file}</div>
                            ))}
                            {changedFiles.length > 8 && (
                              <div className="text-[10px] text-muted-foreground">+{changedFiles.length - 8} more</div>
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export default function Runs() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const { activeWorkspace } = useWorkspace();
  const [runs, setRuns] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const fetchRuns = React.useCallback(() => {
    if (!activeWorkspace) return;
    setLoading(true);
    listRuns({ workspace_id: activeWorkspace.id, limit: 50 }).then(data => {
      setRuns(data.runs);
      setTotal(data.total);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [activeWorkspace]);

  useEffect(() => {
    fetchRuns();
  }, [fetchRuns]);

  const handleDeleted = (deletedRunId) => {
    setRuns(prev => prev.filter(run => run.id !== deletedRunId));
    setTotal(prev => Math.max(0, prev - 1));
  };
  const handleStopped = (stoppedRunId, opts) => {
    if (opts?.revert) {
      fetchRuns();
      return;
    }
    const nextStatus = opts?.optimistic === 'cancelling' ? 'cancelled' : (opts?.status || 'cancelled');
    setRuns(prev => prev.map(run => (
      run.id === stoppedRunId ? { ...run, status: nextStatus } : run
    )));
  };

  return (
    <div className="px-6 lg:px-8 py-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-lg font-semibold tracking-tight">Runs</h2>
          <p className="text-xs text-muted-foreground mt-0.5">{total} total runs</p>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-40">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : runs.length === 0 ? (
        <div className="flex flex-col items-center justify-center h-60 text-center">
          <Play className="h-8 w-8 text-muted-foreground mb-3" />
          <p className="text-sm text-muted-foreground">No runs yet. Start a chat to create your first run.</p>
        </div>
      ) : (
        <div className="rounded-lg border border-border overflow-hidden" data-testid="runs-table">
          {/* Header */}
          <div className="grid grid-cols-[32px_120px_120px_60px_80px_80px_1fr_160px] gap-3 items-center px-4 py-2.5 bg-secondary/30 border-b border-border text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
            <span></span>
            <span>Status</span>
            <span>Model</span>
            <span>Steps</span>
            <span>Cost</span>
            <span>Duration</span>
            <span className="text-right">Created</span>
            <span className="text-right">Actions</span>
          </div>
          {/* Rows */}
          {runs.map(run => (
            <ExpandableRunRow key={run.id} run={run} onDeleted={handleDeleted} onStopped={handleStopped} />
          ))}
        </div>
      )}
    </div>
  );
}
