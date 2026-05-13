import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { Badge } from '../ui/badge';
import {
  User, Zap, ChevronRight, Search, Code, Database,
  FileText, Globe, CheckCircle2, Copy, Brain, Terminal,
  Clock, ArrowRight
} from 'lucide-react';
import { Button } from '../ui/button';
import { toast } from 'sonner';

const toolIcons = {
  web_search: Search,
  python: Code,
  db_query: Database,
  file_read: FileText,
  api_call: Globe,
};

const statusStyles = {
  completed: { dot: 'bg-emerald-500', text: 'text-emerald-400', bg: 'bg-emerald-500/8 border-emerald-500/20' },
  success: { dot: 'bg-emerald-500', text: 'text-emerald-400', bg: 'bg-emerald-500/8 border-emerald-500/20' },
  warning: { dot: 'bg-amber-500', text: 'text-amber-400', bg: 'bg-amber-500/8 border-amber-500/20' },
  failed: { dot: 'bg-rose-500', text: 'text-rose-400', bg: 'bg-rose-500/8 border-rose-500/20' },
  running: { dot: 'bg-blue-500 animate-pulse-dot', text: 'text-blue-400', bg: 'bg-blue-500/8 border-blue-500/20' },
};

function ToolCallBlock({ toolCall, index }) {
  const [expanded, setExpanded] = useState(false);
  const Icon = toolIcons[toolCall.tool] || Terminal;
  const status = statusStyles[toolCall.status] || statusStyles.completed;

  return (
    <motion.div
      data-testid="tool-call-card"
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.16, delay: index * 0.05 }}
      className={`rounded-lg border overflow-hidden ${status.bg}`}
    >
      <button
        data-testid="tool-call-expand-button"
        onClick={() => setExpanded(!expanded)}
        className="flex items-center justify-between w-full px-3 py-2.5 text-left hover:bg-white/[0.02] transition-colors duration-160"
      >
        <div className="flex items-center gap-2.5">
          <div className={`w-2 h-2 rounded-full ${status.dot}`} />
          <Icon className="h-3.5 w-3.5 text-muted-foreground" />
          <span className="text-xs font-mono font-medium">{toolCall.tool}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className={`text-[10px] font-medium ${status.text}`}>{toolCall.status}</span>
          <motion.div
            animate={{ rotate: expanded ? 90 : 0 }}
            transition={{ duration: 0.12 }}
          >
            <ChevronRight className="h-3 w-3 text-muted-foreground" />
          </motion.div>
        </div>
      </button>
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.16 }}
            className="overflow-hidden"
          >
            <div className="px-3 pb-3 border-t border-border/50">
              <pre className="font-mono text-[11px] leading-relaxed text-muted-foreground whitespace-pre-wrap mt-2 max-h-40 overflow-auto">
                {toolCall.output_preview || JSON.stringify(toolCall, null, 2)}
              </pre>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function ReasoningBlock({ content }) {
  return (
    <div className="rounded-lg border border-border/50 bg-secondary/30 overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-border/30 bg-secondary/20">
        <Brain className="h-3 w-3 text-muted-foreground" />
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">Reasoning</span>
      </div>
      <div className="px-3 py-2.5">
        <p className="text-xs text-muted-foreground leading-relaxed">{content}</p>
      </div>
    </div>
  );
}

function CodeBlock({ content, language }) {
  const handleCopy = () => {
    navigator.clipboard.writeText(content);
    toast.success('Code copied');
  };

  return (
    <div className="rounded-lg border border-border/50 bg-[#0A0C10] overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-border/30 bg-[#0F1218]">
        <div className="flex items-center gap-2">
          <Code className="h-3 w-3 text-muted-foreground" />
          <span className="text-[10px] font-mono text-muted-foreground">{language || 'code'}</span>
        </div>
        <Button variant="ghost" size="icon" className="h-5 w-5" onClick={handleCopy}>
          <Copy className="h-2.5 w-2.5" />
        </Button>
      </div>
      <pre className="px-3 py-2.5 text-[11px] font-mono leading-relaxed overflow-x-auto text-emerald-300/80">
        <code>{content}</code>
      </pre>
    </div>
  );
}

export default function MessageCard({ message }) {
  const isUser = message.role === 'user';
  const content = message.content;

  const copyContent = () => {
    let text;
    if (isUser) text = content;
    else if (typeof content === 'string') text = content;
    else if (content?.summary) text = content.summary;
    else text = JSON.stringify(content ?? '');
    navigator.clipboard.writeText(text);
    toast.success('Copied to clipboard');
  };

  if (isUser) {
    return (
      <motion.div
        data-testid="chat-message"
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2 }}
        className="flex justify-end group"
      >
        <div className="max-w-[80%] flex gap-3 items-start">
          <div className="flex-1">
            <div className="flex items-center justify-end gap-2 mb-1.5">
              <span className="text-[10px] text-muted-foreground">{formatTime(message.created_at)}</span>
              <span className="text-xs font-medium">You</span>
            </div>
            <div className="rounded-xl bg-secondary/60 border border-border px-4 py-3">
              <p className="text-sm leading-relaxed whitespace-pre-wrap">{content}</p>
            </div>
          </div>
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-secondary border border-border mt-6">
            <User className="h-3.5 w-3.5 text-muted-foreground" />
          </div>
        </div>
      </motion.div>
    );
  }

  // Assistant: bridge отдаёт plain string; богатый UI — объект { summary, sections, tool_calls, model }.
  const structured = content && typeof content === 'object' && !Array.isArray(content) ? content : null;
  const plainText = typeof content === 'string' ? content : null;

  // Assistant message with structured blocks
  return (
    <motion.div
      data-testid="chat-message"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
      className="flex group"
    >
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-blue-500/10 border border-blue-500/20 mt-6">
        <Zap className="h-3.5 w-3.5 text-blue-400" />
      </div>
      <div className="flex-1 ml-3 min-w-0 max-w-[85%]">
        <div className="flex items-center gap-2 mb-1.5">
          <span className="text-xs font-medium">Umbrella</span>
          {structured?.model && (
            <Badge variant="outline" className="text-[9px] px-1.5 py-0 font-mono">{structured.model}</Badge>
          )}
          <span className="text-[10px] text-muted-foreground">{formatTime(message.created_at)}</span>
        </div>

        <div className="space-y-3">
          {plainText && (
            <div className="rounded-xl bg-card border border-border px-4 py-3 shadow-sm">
              <p className="text-sm leading-relaxed whitespace-pre-wrap">{plainText}</p>
            </div>
          )}
          {/* Summary - main response text */}
          {structured?.summary && (
            <div className="rounded-xl bg-card border border-border px-4 py-3 shadow-sm">
              <p className="text-sm font-medium leading-relaxed">{structured.summary}</p>
            </div>
          )}

          {/* Structured sections */}
          {structured?.sections?.map((section, i) => (
            <div key={i}>
              {section.type === 'text' && (
                <div className="px-1">
                  <p className="text-sm text-muted-foreground leading-relaxed">{section.content}</p>
                </div>
              )}
              {section.type === 'reasoning' && (
                <ReasoningBlock content={section.content} />
              )}
              {section.type === 'code' && (
                <CodeBlock content={section.content} language={section.language} />
              )}
              {section.type === 'list' && (
                <div className="space-y-1.5 px-1">
                  {section.items?.map((item, j) => (
                    <div key={j} className="flex items-start gap-2.5 text-sm text-muted-foreground">
                      <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500 mt-0.5 shrink-0" />
                      <span className="leading-relaxed">{item}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}

          {/* Tool Calls */}
          {structured?.tool_calls?.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center gap-2 px-1">
                <Terminal className="h-3 w-3 text-muted-foreground" />
                <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  Tool Calls ({structured.tool_calls.length})
                </span>
              </div>
              {structured.tool_calls.map((tc, i) => (
                <ToolCallBlock key={i} toolCall={tc} index={i} />
              ))}
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}

function formatTime(ts) {
  if (!ts) return '';
  try {
    return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return '';
  }
}
