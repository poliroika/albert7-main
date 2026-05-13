import React, { useState } from 'react';
import { motion } from 'framer-motion';
import { Button } from '../ui/button';
import { Textarea } from '../ui/textarea';
import { Badge } from '../ui/badge';
import { MessageSquare, ShieldAlert, CheckCircle2, XCircle, Clock, Terminal } from 'lucide-react';
import { answerUserInputRequest, resolvePermissionRequest } from '../../lib/api';
import { toast } from 'sonner';

const permissionIcons = {
  docker: Terminal,
  sudo: ShieldAlert,
  network: ShieldAlert,
  install_package: Terminal,
  custom: ShieldAlert,
};

const permissionColors = {
  docker: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  sudo: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
  network: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  install_package: 'bg-violet-500/10 text-violet-400 border-violet-500/20',
  custom: 'bg-zinc-500/10 text-zinc-400 border-zinc-500/20',
};

/** Карточка запроса комментария/подтверждения от агента */
export function UserInputRequestCard({ request, onAnswered }) {
  const [answer, setAnswer] = useState('');
  const [submitting, setSubmitting] = useState(false);

  if (request.status !== 'pending') return null;

  const handleSubmit = async () => {
    if (!answer.trim() && request.kind !== 'confirmation') return;
    setSubmitting(true);
    try {
      await answerUserInputRequest(request.id, answer.trim() || 'ok');
      toast.success('Ответ отправлен агенту');
      onAnswered?.(request.id, answer);
    } catch (e) {
      toast.error('Не удалось отправить ответ');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="mx-4 my-2 rounded-xl border border-amber-500/25 bg-amber-500/5 p-4"
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-amber-500/10">
          <MessageSquare className="h-3.5 w-3.5 text-amber-400" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-xs font-semibold text-amber-400">Агент ждёт ответа</span>
            <Badge variant="outline" className="text-[10px] bg-amber-500/10 text-amber-400 border-amber-500/20">
              <Clock className="h-2.5 w-2.5 mr-1" />
              {request.kind}
            </Badge>
          </div>
          <p className="text-sm text-foreground/90 leading-relaxed mb-3">{request.prompt}</p>

          {request.kind === 'choice' && request.choices?.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {request.choices.map((choice) => (
                <Button
                  key={choice}
                  size="sm"
                  variant="outline"
                  className="h-7 text-xs border-amber-500/30 hover:bg-amber-500/10 hover:border-amber-500/50"
                  onClick={() => { setAnswer(choice); }}
                  disabled={submitting}
                >
                  {choice}
                </Button>
              ))}
              {answer && (
                <Button
                  size="sm"
                  className="h-7 text-xs bg-amber-500 hover:bg-amber-600 text-black"
                  onClick={handleSubmit}
                  disabled={submitting}
                >
                  Отправить «{answer}»
                </Button>
              )}
            </div>
          ) : request.kind === 'confirmation' ? (
            <div className="flex gap-2">
              <Button
                size="sm"
                className="h-7 text-xs gap-1.5 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                variant="ghost"
                onClick={() => { setAnswer('yes'); handleSubmit(); }}
                disabled={submitting}
              >
                <CheckCircle2 className="h-3 w-3" /> Да
              </Button>
              <Button
                size="sm"
                className="h-7 text-xs gap-1.5 bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/30"
                variant="ghost"
                onClick={() => { setAnswer('no'); handleSubmit(); }}
                disabled={submitting}
              >
                <XCircle className="h-3 w-3" /> Нет
              </Button>
            </div>
          ) : (
            <div className="flex gap-2">
              <Textarea
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                placeholder="Введите ответ агенту..."
                className="min-h-[60px] text-sm resize-none bg-background/50"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) handleSubmit();
                }}
              />
              <Button
                size="sm"
                className="h-auto px-4 text-xs self-end"
                onClick={handleSubmit}
                disabled={!answer.trim() || submitting}
              >
                Отправить
              </Button>
            </div>
          )}
        </div>
      </div>
    </motion.div>
  );
}

/** Карточка запроса повышения прав */
export function PermissionRequestCard({ request, onResolved }) {
  const [resolving, setResolving] = useState(false);
  if (request.status !== 'pending') return null;

  const Icon = permissionIcons[request.permission_type] || ShieldAlert;
  const colorClass = permissionColors[request.permission_type] || permissionColors.custom;

  const handle = async (granted) => {
    setResolving(true);
    try {
      await resolvePermissionRequest(request.id, granted);
      toast.success(granted ? 'Права предоставлены' : 'Запрос отклонён');
      onResolved?.(request.id, granted);
    } catch (e) {
      toast.error('Ошибка при обработке запроса');
    } finally {
      setResolving(false);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className="mx-4 my-2 rounded-xl border border-rose-500/25 bg-rose-500/5 p-4"
    >
      <div className="flex items-start gap-3">
        <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-rose-500/10">
          <ShieldAlert className="h-3.5 w-3.5 text-rose-400" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1.5">
            <span className="text-xs font-semibold text-rose-400">Запрос прав</span>
            <Badge variant="outline" className={`text-[10px] ${colorClass}`}>
              <Icon className="h-2.5 w-2.5 mr-1" />
              {request.permission_type}
            </Badge>
          </div>
          <p className="text-sm text-foreground/90 leading-relaxed mb-2">{request.description}</p>
          {request.command && (
            <pre className="mb-3 rounded-lg bg-background/60 border border-border/50 px-3 py-2 text-xs font-mono text-muted-foreground overflow-x-auto">
              {request.command}
            </pre>
          )}
          <div className="flex gap-2">
            <Button
              size="sm"
              className="h-7 text-xs gap-1.5 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
              variant="ghost"
              onClick={() => handle(true)}
              disabled={resolving}
            >
              <CheckCircle2 className="h-3 w-3" /> Разрешить
            </Button>
            <Button
              size="sm"
              className="h-7 text-xs gap-1.5 bg-rose-500/10 hover:bg-rose-500/20 text-rose-400 border border-rose-500/30"
              variant="ghost"
              onClick={() => handle(false)}
              disabled={resolving}
            >
              <XCircle className="h-3 w-3" /> Отклонить
            </Button>
          </div>
        </div>
      </div>
    </motion.div>
  );
}
