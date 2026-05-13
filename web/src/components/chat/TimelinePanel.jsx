import React from 'react';
import { motion } from 'framer-motion';
import { ScrollArea } from '../ui/scroll-area';
import { Badge } from '../ui/badge';
import {
  Search, Code, Database, FileText, Globe,
  Brain, MessageSquare, CheckCircle2, XCircle,
  AlertCircle, Clock, Loader2, Zap, Terminal
} from 'lucide-react';
import HarnessProgressPanel from './HarnessProgressPanel';

const toolIcons = {
  web_search: Search,
  python: Code,
  db_query: Database,
  file_read: FileText,
  api_call: Globe,
};

const stepIcons = {
  thinking: Brain,
  tool_call: Terminal,
  response: MessageSquare,
};

const statusConfig = {
  queued: { color: 'bg-zinc-500', dotColor: '#71717a', label: 'Queued' },
  running: { color: 'bg-blue-500 animate-pulse-dot', dotColor: '#3b82f6', label: 'Running' },
  completed: { color: 'bg-emerald-500', dotColor: '#22c55e', label: 'Completed' },
  failed: { color: 'bg-rose-500', dotColor: '#ef4444', label: 'Failed' },
  cancelled: { color: 'bg-amber-500', dotColor: '#f59e0b', label: 'Cancelled' },
  warning: { color: 'bg-amber-500', dotColor: '#f59e0b', label: 'Warning' },
};

const runStatusBadge = {
  queued: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/20',
  running: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  completed: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
  failed: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
  cancelled: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
};

const panelShell = 'w-full min-w-0 h-full border-l border-border bg-card/50 flex flex-col';

export default function TimelinePanel({ run, steps }) {
  if (!run) {
    return (
      <div
        data-testid="execution-timeline-panel"
        className={panelShell}
      >
        <div className="p-4 border-b border-border">
          <h3 className="text-sm font-semibold">Execution Timeline</h3>
        </div>
        <div className="flex-1 flex items-center justify-center">
          <div className="text-center px-6">
            <div className="w-12 h-12 rounded-xl bg-secondary flex items-center justify-center mx-auto mb-3">
              <Zap className="h-5 w-5 text-muted-foreground" />
            </div>
            <p className="text-xs text-muted-foreground leading-relaxed">
              Send a message to see the<br />execution timeline
            </p>
          </div>
        </div>
      </div>
    );
  }

  const runStatus = statusConfig[run.status] || statusConfig.queued;

  // Group steps into rounds
  let roundNum = 0;
  const stepsWithRounds = steps.map((step, i) => {
    if (step.type === 'thinking' || (i === 0)) roundNum++;
    else if (step.type === 'tool_call' && i > 0 && steps[i-1]?.type === 'response') roundNum++;
    return { ...step, round: roundNum };
  });

  const isHarness = run.mode === 'harness' || !!run.harness_meta;

  return (
    <div
      data-testid="execution-timeline-panel"
      className={panelShell}
    >
      {/* Run header */}
      <div className="p-4 border-b border-border">
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-sm font-semibold">
            {isHarness ? 'Harness Timeline' : 'Execution Timeline'}
          </h3>
          <Badge
            data-testid="run-status-pill"
            variant="outline"
            className={`text-[10px] px-2 py-0.5 gap-1.5 font-medium ${runStatusBadge[run.status] || ''}`}
          >
            <div className={`w-1.5 h-1.5 rounded-full ${runStatus.color}`} />
            {runStatus.label}
          </Badge>
        </div>
        <div className="flex items-center gap-4 text-[11px] text-muted-foreground">
          <span className="tabular-nums">{steps.length} steps</span>
          {run.total_cost > 0 && <span className="tabular-nums">${run.total_cost.toFixed(4)}</span>}
          {run.model && <span className="font-mono">{run.model}</span>}
          {isHarness && run.harness_meta?.candidates && (
            <span className="font-mono text-purple-300">
              {run.harness_meta.candidates} candidates
            </span>
          )}
        </div>
      </div>

      {isHarness && <HarnessProgressPanel run={run} />}

      {/* Steps timeline */}
      <ScrollArea className="flex-1">
        <div className="p-4">
          {stepsWithRounds.map((step, i) => {
            const isLast = i === stepsWithRounds.length - 1;
            const showRoundHeader = i === 0 || step.round !== stepsWithRounds[i - 1]?.round;
            const stepStatus = statusConfig[step.status] || statusConfig.completed;
            const StepIcon = step.type === 'tool_call'
              ? (toolIcons[step.name] || Terminal)
              : (stepIcons[step.type] || Zap);

            return (
              <div key={step.id || i}>
                {/* Round header */}
                {showRoundHeader && (
                  <div className="flex items-center gap-2 mb-2 mt-1">
                    <span className="text-[9px] font-semibold uppercase tracking-wider text-muted-foreground/60">
                      Round {step.round}
                    </span>
                    <div className="flex-1 h-px bg-border/50" />
                  </div>
                )}

                <motion.div
                  data-testid="execution-timeline-step"
                  initial={{ opacity: 0, x: -6 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ duration: 0.16, delay: i * 0.04 }}
                  className="relative pl-7 pb-4 last:pb-0"
                >
                  {/* Vertical line */}
                  {!isLast && (
                    <div
                      className="absolute left-[9px] top-[18px] bottom-0 w-[2px] rounded-full"
                      style={{ background: `linear-gradient(to bottom, ${stepStatus.dotColor}40, ${stepStatus.dotColor}10)` }}
                    />
                  )}

                  {/* Status dot */}
                  <div
                    className={`absolute left-[4px] top-[6px] h-[12px] w-[12px] rounded-full border-2 ${stepStatus.color}`}
                    style={{ borderColor: stepStatus.dotColor, background: `${stepStatus.dotColor}30` }}
                  />

                  {/* Step content */}
                  <div className="rounded-lg border border-border/50 bg-secondary/20 px-3 py-2.5 hover:bg-secondary/40 transition-colors duration-160">
                    <div className="flex items-center justify-between mb-1">
                      <div className="flex items-center gap-2">
                        <StepIcon className="h-3.5 w-3.5 text-muted-foreground" />
                        <span className="text-xs font-medium">
                          {step.type === 'tool_call' ? step.name : step.name || step.type}
                        </span>
                      </div>
                      <span className="text-[10px] text-muted-foreground tabular-nums font-mono">
                        {step.duration_ms ? `${step.duration_ms}ms` : ''}
                      </span>
                    </div>

                    {/* Step detail */}
                    {(step.data || (step.type === 'tool_call' && step.input)) && (
                      <div className="mt-1.5 rounded bg-[#0A0C10] border border-border/30 px-2 py-1.5">
                        <pre className="font-mono text-[10px] text-muted-foreground/80 whitespace-pre-wrap line-clamp-2">
                          {step.data || (typeof step.input === 'string' ? step.input : JSON.stringify(step.input, null, 2).slice(0, 120))}
                        </pre>
                      </div>
                    )}

                    {/* Cost */}
                    {step.cost > 0 && (
                      <div className="mt-1.5 flex items-center gap-1.5">
                        <span className="text-[9px] text-muted-foreground/60 tabular-nums">${step.cost.toFixed(4)}</span>
                      </div>
                    )}
                  </div>
                </motion.div>
              </div>
            );
          })}

          {/* Running indicator */}
          {(run.status === 'running' || run.status === 'queued') && (
            <div className="relative pl-7 pt-2">
              <div className="absolute left-[4px] top-[14px] h-[12px] w-[12px] rounded-full bg-blue-500/30 border-2 border-blue-500 animate-pulse-dot" />
              <div className="flex items-center gap-2 text-xs text-blue-400">
                <Loader2 className="h-3 w-3 animate-spin" />
                <span>Processing...</span>
              </div>
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
