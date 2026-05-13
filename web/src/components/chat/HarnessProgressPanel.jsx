import React from 'react';
import { motion } from 'framer-motion';
import { ScrollArea } from '../ui/scroll-area';
import { Badge } from '../ui/badge';
import {
  AlertCircle,
  Bug,
  CheckCircle2,
  Clock,
  Crown,
  GitBranch,
  GitMerge,
  Layers,
  ListChecks,
  Loader2,
  Scissors,
  Search,
  ShieldCheck,
  Trophy,
  XCircle,
} from 'lucide-react';

const candidateStatusBadge = {
  pending: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/20',
  running: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  completed: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
  recovered: 'bg-cyan-500/10 text-cyan-300 border-cyan-500/25',
  failed: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
  cancelled: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
};

const statusIcon = {
  pending: Clock,
  running: Loader2,
  completed: CheckCircle2,
  recovered: ShieldCheck,
  failed: XCircle,
  cancelled: AlertCircle,
};

const stageIcon = {
  planning: Search,
  research: Search,
  subtask: ListChecks,
  implementation: Layers,
  bugfix: Bug,
  remediation: Bug,
  verification: ShieldCheck,
  final: Trophy,
};

const candidateStrategies = [
  {
    title: 'Evidence-first',
    summary: 'Collects requirements, docs, examples, and success criteria before changing files.',
  },
  {
    title: 'Minimal-risk',
    summary: 'Takes the smallest reversible patch with focused verification and low blast radius.',
  },
  {
    title: 'Integration-first',
    summary: 'Traces UI, bridge, memory, logs, and runtime handoffs before patching.',
  },
  {
    title: 'Reviewer',
    summary: 'Looks for hidden failure modes, regressions, and weak assumptions.',
  },
];

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function normalizeCandidates(stage) {
  const candidates = [];
  const seen = new Set();
  for (const candidate of asArray(stage?.candidates)) {
    if (!candidate || typeof candidate !== 'object') continue;
    const key = String(candidate.candidate_id || candidate.run_id || candidate.index);
    seen.add(key);
    candidates.push(candidate);
  }
  for (const score of asArray(stage?.scores)) {
    if (!score || typeof score !== 'object') continue;
    const key = String(score.candidate_id || score.run_id || score.index);
    const existing = candidates.find((item) => (
      String(item.candidate_id || item.run_id || item.index) === key
    ));
    if (existing) {
      Object.assign(existing, score);
      continue;
    }
    if (seen.has(key)) continue;
    seen.add(key);
    candidates.push({
      candidate_id: score.candidate_id,
      run_id: score.run_id,
      status: score.status,
      score: score.score,
      score_breakdown: score.breakdown,
    });
  }
  return candidates;
}

function normalizeStages(run) {
  const result = run?.full_result?.harness_result || {};
  const meta = run?.harness_meta || {};
  const rawStages = asArray(run?.harness_stages).length
    ? asArray(run.harness_stages)
    : (asArray(meta.stages).length ? asArray(meta.stages) : asArray(result.stages));
  return rawStages.map((stage, index) => ({
    ...stage,
    index: stage.index ?? index,
    stage_id: stage.stage_id || `s${index + 1}`,
    title: stage.title || `Stage ${index + 1}`,
    kind: stage.kind || 'subtask',
    status: stage.status || 'pending',
    candidates: normalizeCandidates(stage),
  }));
}

function fallbackCandidates(run) {
  const meta = run?.harness_meta || run?.full_result?.harness_result || {};
  const candidatesFromMeta = asArray(meta.candidates);
  const scoresFromRun = asArray(run?.harness_scores);
  const progress = asArray(run?.harness_candidate_progress);
  const cards = [];
  const seen = new Set();
  for (const candidate of candidatesFromMeta) {
    if (!candidate || typeof candidate !== 'object') continue;
    const key = String(candidate.candidate_id || candidate.run_id || candidate.index);
    seen.add(key);
    cards.push(candidate);
  }
  for (const score of scoresFromRun) {
    const key = String(score.candidate_id || score.run_id || score.index);
    if (seen.has(key)) continue;
    seen.add(key);
    cards.push({
      candidate_id: score.candidate_id,
      status: score.status,
      score: score.score,
      score_breakdown: score.breakdown,
    });
  }
  for (const item of progress) {
    const key = String(item.candidate_id || item.candidate_index);
    if (seen.has(key)) continue;
    seen.add(key);
    cards.push({
      candidate_id: item.candidate_id || `c${(item.candidate_index ?? 0) + 1}`,
      status: 'completed',
      score: item.data?.score,
      duration_ms: item.data?.duration_ms,
    });
  }
  return cards;
}

function CandidateCard({ candidate, isWinner, isPruned }) {
  const status = candidate.status || 'pending';
  const Icon = statusIcon[status] || Clock;
  const score = typeof candidate.score === 'number' ? candidate.score : null;
  const breakdown = candidate.breakdown || candidate.score_breakdown || {};
  const breakdownEntries = Object.entries(breakdown).slice(0, 4);
  const fallbackStrategy = candidateStrategies[(candidate.index ?? 0) % candidateStrategies.length] || {};
  const strategyTitle = candidate.strategy_title || candidate.strategy?.title || candidate.strategy_id || fallbackStrategy.title || '';
  const strategySummary = candidate.strategy_summary || candidate.strategy?.summary || fallbackStrategy.summary || '';
  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.16 }}
      className={`min-w-0 rounded-md border px-2.5 py-2 ${
        isWinner
          ? 'border-emerald-400/55 bg-emerald-500/10'
          : isPruned
            ? 'border-zinc-700/70 bg-zinc-900/35 opacity-75'
            : 'border-border/50 bg-background/35'
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5 min-w-0">
          {isWinner ? (
            <Crown className="h-3.5 w-3.5 shrink-0 text-emerald-300" />
          ) : (
            <Layers className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          )}
          <span className="truncate text-[11px] font-semibold">
            {candidate.candidate_id || `c${(candidate.index ?? 0) + 1}`}
          </span>
        </div>
        <Badge
          variant="outline"
          className={`gap-1 text-[9px] ${candidateStatusBadge[status] || candidateStatusBadge.pending}`}
        >
          <Icon className={`h-2.5 w-2.5 ${status === 'running' ? 'animate-spin' : ''}`} />
          {status}
        </Badge>
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
        <span className="truncate font-mono">{candidate.model || candidate.run_id || 'default'}</span>
        {score !== null && (
          <span className="tabular-nums font-semibold text-foreground">
            {score.toFixed(2)}
          </span>
        )}
      </div>
      {(strategyTitle || strategySummary) && (
        <div className="mt-1.5 rounded border border-blue-400/20 bg-blue-500/5 px-1.5 py-1">
          <div className="flex min-w-0 items-center gap-1 text-[9px] font-semibold uppercase text-blue-200">
            <GitBranch className="h-2.5 w-2.5 shrink-0" />
            <span className="truncate">{strategyTitle || 'strategy'}</span>
          </div>
          {strategySummary && (
            <p className="mt-0.5 line-clamp-2 text-[9px] leading-tight text-muted-foreground">
              {strategySummary}
            </p>
          )}
        </div>
      )}
      {breakdownEntries.length > 0 && (
        <div className="mt-1.5 grid grid-cols-2 gap-1">
          {breakdownEntries.map(([key, value]) => (
            <div
              key={key}
              title={key}
              className="flex min-w-0 justify-between gap-1 rounded bg-background/45 px-1.5 py-0.5 font-mono text-[9px] text-muted-foreground"
            >
              <span className="truncate">{key}</span>
              <span className="tabular-nums">{Number(value).toFixed(1)}</span>
            </div>
          ))}
        </div>
      )}
      {candidate.error && (
        <div className="mt-1.5 rounded border border-rose-500/25 bg-rose-500/10 px-1.5 py-1 font-mono text-[9px] text-rose-300">
          {candidate.error.slice(0, 180)}
        </div>
      )}
    </motion.div>
  );
}

function StageBlock({ stage, totalCandidates }) {
  const Icon = stageIcon[stage.kind] || ListChecks;
  const candidates = stage.candidates || [];
  const winnerIndex = typeof stage.winner_index === 'number' ? stage.winner_index : null;
  const winnerId = stage.winner_id || (
    winnerIndex !== null ? candidates[winnerIndex]?.candidate_id : ''
  );
  const pruned = new Set(asArray(stage.pruned_candidate_ids).map(String));
  const StatusIcon = statusIcon[stage.status] || Clock;
  return (
    <div className="rounded-md border border-border/55 bg-secondary/15 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2">
          <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border/60 bg-background/50">
            <Icon className="h-3.5 w-3.5 text-blue-300" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="truncate text-xs font-semibold">
                {stage.index + 1}. {stage.title}
              </span>
              <Badge variant="outline" className="text-[9px] uppercase text-muted-foreground">
                {stage.kind}
              </Badge>
            </div>
            {stage.description && (
              <p className="mt-1 line-clamp-2 text-[10px] leading-snug text-muted-foreground">
                {stage.description}
              </p>
            )}
          </div>
        </div>
        <Badge
          variant="outline"
          className={`shrink-0 gap-1 text-[9px] ${candidateStatusBadge[stage.status] || candidateStatusBadge.pending}`}
        >
          <StatusIcon className={`h-2.5 w-2.5 ${stage.status === 'running' ? 'animate-spin' : ''}`} />
          {stage.status}
        </Badge>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
        <span className="inline-flex items-center gap-1 rounded border border-blue-400/25 bg-blue-500/10 px-2 py-0.5 text-blue-200">
          <GitBranch className="h-3 w-3" />
          split {candidates.length || totalCandidates || 0}
        </span>
        {winnerId && (
          <span className="inline-flex items-center gap-1 rounded border border-emerald-400/25 bg-emerald-500/10 px-2 py-0.5 text-emerald-200">
            <GitMerge className="h-3 w-3" />
            winner {winnerId}
          </span>
        )}
        {pruned.size > 0 && (
          <span className="inline-flex items-center gap-1 rounded border border-zinc-500/25 bg-zinc-500/10 px-2 py-0.5 text-zinc-300">
            <Scissors className="h-3 w-3" />
            pruned {pruned.size}
          </span>
        )}
        {stage.recovered && (
          <span className="inline-flex items-center gap-1 rounded border border-cyan-400/25 bg-cyan-500/10 px-2 py-0.5 text-cyan-200">
            <ShieldCheck className="h-3 w-3" />
            recovered
          </span>
        )}
      </div>

      {candidates.length > 0 && (
        <div className="mt-2 grid grid-cols-1 gap-2 xl:grid-cols-2">
          {candidates.map((candidate, index) => {
            const candidateId = String(candidate.candidate_id || candidate.run_id || index);
            return (
              <CandidateCard
                key={candidateId}
                candidate={{ ...candidate, index: candidate.index ?? index }}
                isWinner={
                  (winnerIndex !== null && (candidate.index ?? index) === winnerIndex)
                  || (winnerId && String(candidate.candidate_id) === String(winnerId))
                }
                isPruned={pruned.has(String(candidate.candidate_id))}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function HarnessProgressPanel({ run }) {
  const result = run?.full_result?.harness_result || {};
  const meta = run?.harness_meta || {};
  const stages = normalizeStages(run);
  const fallbackCards = stages.length ? [] : fallbackCandidates(run);
  const stageCount = stages.length || meta.stage_count || result.stages?.length || 0;
  const totalCandidates = meta.candidates || result.num_candidates || run?.harness_meta?.candidates || 0;
  const timeline = asArray(run?.harness_timeline).slice(-5).reverse();
  const finalMessage =
    run?.full_result?.final_message ||
    result.final_message ||
    run?.result_preview ||
    '';

  return (
    <div className="border-b border-border/60 bg-secondary/10" data-testid="harness-progress-panel">
      <div className="px-4 pt-3 pb-2">
        <div className="flex items-center justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2">
            <Trophy className="h-3.5 w-3.5 shrink-0 text-emerald-300" />
            <span className="truncate text-xs font-semibold tracking-tight">Staged harness</span>
            <Badge variant="outline" className="text-[10px] border-blue-400/35 text-blue-200">
              {stageCount} stages
            </Badge>
          </div>
          <Badge variant="outline" className="shrink-0 text-[10px] border-emerald-400/35 text-emerald-200">
            {totalCandidates || fallbackCards.length || 0} per split
          </Badge>
        </div>
      </div>

      <ScrollArea className="max-h-[360px]">
        <div className="space-y-2 px-4 pb-3">
          {stages.length > 0 ? (
            stages.map((stage) => (
              <StageBlock
                key={stage.stage_id || stage.index}
                stage={stage}
                totalCandidates={totalCandidates}
              />
            ))
          ) : fallbackCards.length > 0 ? (
            fallbackCards.map((candidate, index) => (
              <CandidateCard
                key={candidate.candidate_id || index}
                candidate={{ ...candidate, index: candidate.index ?? index }}
                isWinner={typeof meta.winner_index === 'number' && (candidate.index ?? index) === meta.winner_index}
                isPruned={false}
              />
            ))
          ) : (
            <div className="rounded-md border border-border/50 bg-background/35 px-3 py-2 text-[11px] text-muted-foreground">
              Waiting for the first harness stage event...
            </div>
          )}
        </div>
      </ScrollArea>

      {timeline.length > 0 && (
        <div className="border-t border-border/50 px-4 py-2">
          <div className="space-y-1">
            {timeline.map((event, index) => (
              <div key={`${event.ts}-${event.type}-${index}`} className="flex min-w-0 items-center gap-2 text-[10px] text-muted-foreground">
                <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-blue-400/70" />
                <span className="truncate">
                  {event.stage_title ? `${event.stage_title}: ` : ''}{event.message || event.type}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {finalMessage && (
        <div className="px-4 pb-3 text-[11px] leading-snug text-muted-foreground">
          {finalMessage}
        </div>
      )}
    </div>
  );
}
