import React, { useState, useRef, useEffect } from 'react';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import {
  Send, Square, ChevronDown, Check, Play, Layers,
  Search, Code, Database, FileText, Globe
} from 'lucide-react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../ui/dropdown-menu';

const toolIcons = {
  web_search: Search,
  python: Code,
  db_query: Database,
  file_read: FileText,
  api_call: Globe,
};

export default function Composer({
  onSend, sending, models, tools,
  selectedModel, onModelChange, onRunTaskMain,
  maxRounds, maxVerifyRetries, onMaxRoundsChange, onMaxVerifyRetriesChange,
  onCancelRun, cancelEnabled,
  harnessMode = false, onHarnessModeChange,
  harnessCandidates = 3, onHarnessCandidatesChange,
}) {
  const [input, setInput] = useState('');
  const textareaRef = useRef(null);

  const handleSubmit = (e) => {
    e?.preventDefault();
    if (input.trim() && !sending) {
      onSend(input.trim());
      setInput('');
    }
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = Math.min(textareaRef.current.scrollHeight, 180) + 'px';
    }
  }, [input]);

  const activeModel = models.find(m => m.id === selectedModel);

  return (
    <div data-testid="chat-composer" className="border-t border-border/70 bg-background/80 backdrop-blur px-4 lg:px-8 py-3">
      <div className="max-w-3xl mx-auto">
        {/* Controls row */}
        <div className="flex flex-wrap items-center gap-2 mb-2">
          {/* Model selector */}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-7 text-xs gap-1.5"
            onClick={onRunTaskMain}
            disabled={sending}
            data-testid="run-task-main-button"
          >
            <Play className="h-3 w-3" />
            TASK_MAIN.md
          </Button>

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs gap-1.5"
                data-testid="model-selector"
              >
                <span className="truncate max-w-[120px]">{activeModel?.name || selectedModel}</span>
                <ChevronDown className="h-3 w-3 opacity-50" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              {models.map(m => (
                <DropdownMenuItem key={m.id} onClick={() => onModelChange(m.id)}>
                  <div className="flex items-center gap-2">
                    {m.id === selectedModel && <Check className="h-3 w-3" />}
                    <div>
                      <p className="text-sm">{m.name}</p>
                      <p className="text-xs text-muted-foreground">{m.provider} · {m.context_window?.toLocaleString()} ctx</p>
                    </div>
                  </div>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>

          <label className="flex h-7 items-center gap-1.5 rounded-md border border-border bg-background px-2 text-[11px] text-muted-foreground">
            <span>Rounds</span>
            <Input
              type="number"
              min="0"
              step="1"
              value={maxRounds}
              onChange={(e) => onMaxRoundsChange(e.target.value)}
              disabled={sending}
              className="h-5 w-16 border-0 bg-transparent px-0 py-0 text-xs text-foreground focus-visible:ring-0"
              data-testid="max-rounds-input"
              title="0 = unlimited rounds"
            />
          </label>

          <label className="flex h-7 items-center gap-1.5 rounded-md border border-border bg-background px-2 text-[11px] text-muted-foreground">
            <span>Retries</span>
            <Input
              type="number"
              min="0"
              step="1"
              value={maxVerifyRetries}
              onChange={(e) => onMaxVerifyRetriesChange(e.target.value)}
              disabled={sending}
              className="h-5 w-12 border-0 bg-transparent px-0 py-0 text-xs text-foreground focus-visible:ring-0"
              data-testid="max-verify-retries-input"
              title="Verification remediation attempts inside this run; backend default is 20 and can also be set with OUROBOROS_WEB_MAX_VERIFY_RETRIES"
            />
          </label>

          <Button
            type="button"
            variant={harnessMode ? 'default' : 'outline'}
            size="sm"
            className={`h-7 text-xs gap-1.5 ${harnessMode ? 'bg-purple-500/80 hover:bg-purple-500 text-white' : ''}`}
            onClick={() => onHarnessModeChange?.(!harnessMode)}
            disabled={sending}
            data-testid="harness-mode-toggle"
            title="Phase-level harness: внутри каждой фазы запускается N кандидатов параллельно, Watcher выбирает лучшего и продвигает его в следующую фазу. Долгая, дорогая операция."
          >
            <Layers className="h-3 w-3" />
            Harness {harnessMode ? 'ON' : 'mode'}
          </Button>

          {harnessMode && (
            <label
              className="flex h-7 items-center gap-1.5 rounded-md border border-purple-500/40 bg-purple-500/10 px-2 text-[11px] text-purple-200"
              title="Сколько параллельных кандидатов запускать на каждой фазе (2-8)"
            >
              <span>Кандидатов / фазу</span>
              <Input
                type="number"
                min="2"
                max="8"
                step="1"
                value={harnessCandidates}
                onChange={(e) => onHarnessCandidatesChange?.(e.target.value)}
                disabled={sending}
                className="h-5 w-12 border-0 bg-transparent px-0 py-0 text-xs text-foreground focus-visible:ring-0"
                data-testid="harness-candidates-input"
              />
            </label>
          )}

          {/* Tool reference */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs gap-1.5"
                data-testid="tool-selector"
              >
                <span>{tools.length} tools</span>
                <ChevronDown className="h-3 w-3 opacity-50" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-[360px] max-h-[420px] overflow-y-auto">
              {tools.map(t => {
                const Icon = toolIcons[t.id] || Code;
                return (
                  <DropdownMenuItem
                    key={t.id}
                    onSelect={(event) => event.preventDefault()}
                    className="items-start gap-2 py-2"
                  >
                    <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                    <div className="min-w-0">
                      <p className="truncate text-xs font-medium">{t.name || t.id}</p>
                      {t.desc && (
                        <p className="mt-0.5 whitespace-normal break-words text-[11px] leading-snug text-muted-foreground">
                          {t.desc}
                        </p>
                      )}
                    </div>
                  </DropdownMenuItem>
                );
              })}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>

        {/* Input area */}
        <form onSubmit={handleSubmit} className="flex gap-2 items-end">
          <div className="flex-1 relative">
            <textarea
              ref={textareaRef}
              data-testid="chat-prompt-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask Umbrella anything..."
              className="w-full min-h-[48px] max-h-[180px] resize-none rounded-lg border border-border bg-secondary/30 px-4 py-3.5 text-sm leading-relaxed placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-ring/30 focus:border-ring/50 transition-all duration-160"
              rows={1}
            />
          </div>
          {cancelEnabled ? (
            <Button
              type="button"
              variant="destructive"
              size="icon"
              className="h-[48px] w-[48px] shrink-0 rounded-lg"
              onClick={() => onCancelRun?.()}
              data-testid="chat-cancel-run-button"
              title="Stop current run"
            >
              <Square className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              data-testid="chat-run-button"
              type="submit"
              size="icon"
              disabled={!input.trim() || sending}
              className="h-[48px] w-[48px] shrink-0 rounded-lg bg-blue-500 hover:bg-blue-600 text-white"
            >
              {sending ? <Square className="h-4 w-4" /> : <Send className="h-4 w-4" />}
            </Button>
          )}
        </form>
      </div>
    </div>
  );
}
