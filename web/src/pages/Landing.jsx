import React from 'react';
import { Link } from 'react-router-dom';
import { useTheme } from '../context/ThemeContext';
import { Button } from '../components/ui/button';
import {
  Zap, ArrowRight, MessageSquare, GitBranch, Play, FileText,
  BarChart3, Sun, Moon, Check, ShieldAlert, Brain
} from 'lucide-react';
import { motion } from 'framer-motion';

const features = [
  {
    icon: MessageSquare,
    title: 'Чат с агентом',
    description: 'Отправляйте задачи агенту в привычном интерфейсе чата. Каждый ответ содержит полную информацию о ходе выполнения — инструменты, рассуждения, результат.',
  },
  {
    icon: Play,
    title: 'Таймлайн выполнения',
    description: 'Наблюдайте за каждым шагом работы агента в реальном времени: от размышлений до вызовов инструментов и финального ответа.',
  },
  {
    icon: GitBranch,
    title: 'Граф памяти',
    description: 'Интерактивный граф знаний, который строится из разговоров и результатов задач. Агент помнит контекст между сессиями.',
  },
  {
    icon: ShieldAlert,
    title: 'Управление правами',
    description: 'Агент может запрашивать повышение прав прямо во время выполнения — установку Docker, sudo и т.д. Вы подтверждаете или отклоняете запрос через UI.',
  },
  {
    icon: BarChart3,
    title: 'Аналитика запусков',
    description: 'Отслеживайте стоимость, процент успешных запусков и производительность по всей истории работы агента.',
  },
  {
    icon: Brain,
    title: 'Самоулучшение',
    description: 'Umbrella анализирует свою работу и улучшает собственный код. Control plane управляет циклом самосовершенствования агента.',
  },
];

const capabilities = [
  'Таймлайн выполнения в реальном времени',
  'Структурированные ответы с инструментами',
  'Запросы комментариев во время задачи',
  'Управление правами агента из UI',
  'Граф памяти с историей контекста',
  'Отслеживание стоимости каждого запуска',
  'Поддержка нескольких рабочих пространств',
  'Система логов с фильтрацией и поиском',
];

export default function Landing() {
  const { theme, toggleTheme } = useTheme();

  return (
    <div className="min-h-screen bg-background">
      {/* Navigation */}
      <nav className="sticky top-0 z-50 border-b border-border/50 bg-background/70 backdrop-blur supports-[backdrop-filter]:bg-background/50">
        <div className="max-w-[1200px] mx-auto flex items-center justify-between h-14 px-4 sm:px-6">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-md bg-blue-500/10 border border-blue-500/20">
              <Zap className="h-4 w-4 text-blue-400" />
            </div>
            <span className="text-base font-semibold tracking-tight">Umbrella</span>
          </div>
          <div className="flex items-center gap-3">
            <Button variant="ghost" size="icon" className="h-8 w-8" onClick={toggleTheme} data-testid="theme-toggle">
              {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>
            <Link to="/chat">
              <Button size="sm" className="h-8 gap-1.5" data-testid="landing-cta">
                Открыть <ArrowRight className="h-3.5 w-3.5" />
              </Button>
            </Link>
          </div>
        </div>
      </nav>

      {/* Hero Section */}
      <section className="relative overflow-hidden">
        <div
          className="absolute inset-0 pointer-events-none"
          style={{
            background: 'radial-gradient(60% 60% at 20% 10%, rgba(34,211,238,0.12) 0%, rgba(34,211,238,0) 60%), radial-gradient(50% 50% at 80% 0%, rgba(52,211,153,0.08) 0%, rgba(52,211,153,0) 55%)'
          }}
        />

        <div className="max-w-[1200px] mx-auto px-4 sm:px-6 pt-20 pb-16 md:pt-28 md:pb-24">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5 }}
            className="text-center max-w-3xl mx-auto"
          >
            <div className="inline-flex items-center gap-2 rounded-full border border-border bg-card/60 px-3 py-1 mb-6">
              <Zap className="h-3 w-3 text-blue-400" />
              <span className="text-xs text-muted-foreground">AI-агент с самоулучшением</span>
            </div>
            <h1 className="text-4xl sm:text-5xl lg:text-6xl font-semibold tracking-tight mb-4 leading-[1.1]">
              Агент, который работает,
              <br />
              <span className="text-blue-400">
                учится и улучшается
              </span>
            </h1>
            <p className="text-base md:text-lg text-muted-foreground max-w-2xl mx-auto mb-4 leading-relaxed">
              Umbrella — система управления AI-агентом с полным контролем выполнения. Агент принимает задачи, использует инструменты, запрашивает права и права — всё через единый интерфейс.
            </p>
            <p className="text-sm text-muted-foreground/70 max-w-xl mx-auto mb-8 leading-relaxed">
              Построен на базе Ouroboros LLM-движка. Поддерживает любые совместимые с OpenAI API модели. Хранит долгосрочную память между сессиями и анализирует собственные ошибки.
            </p>
            <div className="flex items-center justify-center gap-3">
              <Link to="/chat">
                <Button size="lg" className="gap-2 h-11 px-6" data-testid="hero-cta">
                  Начать работу <ArrowRight className="h-4 w-4" />
                </Button>
              </Link>
              <Link to="/dashboard">
                <Button variant="outline" size="lg" className="h-11 px-6">
                  Дашборд
                </Button>
              </Link>
            </div>
          </motion.div>

          {/* UI Preview Mock */}
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, delay: 0.2 }}
            className="mt-16 mx-auto max-w-4xl"
          >
            <div className="rounded-xl border border-border/70 bg-card/60 shadow-2xl overflow-hidden">
              <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border/70 bg-card/40">
                <div className="flex gap-1.5">
                  <div className="w-3 h-3 rounded-full bg-rose-500/40" />
                  <div className="w-3 h-3 rounded-full bg-amber-500/40" />
                  <div className="w-3 h-3 rounded-full bg-emerald-500/40" />
                </div>
                <span className="text-[10px] text-muted-foreground/60 ml-2 font-mono">umbrella — chat workspace</span>
              </div>
              <div className="flex h-[340px]">
                {/* Sidebar mock */}
                <div className="w-[180px] border-r border-border/40 p-3 space-y-2 hidden sm:block">
                  {['Чат', 'Граф памяти', 'Запуски', 'Логи', 'Дашборд'].map((item, i) => (
                    <div key={i} className={`rounded-md px-2.5 py-1.5 text-xs ${
                      i === 0 ? 'bg-accent text-foreground' : 'text-muted-foreground/60'
                    }`}>{item}</div>
                  ))}
                </div>
                {/* Chat mock */}
                <div className="flex-1 flex flex-col p-4">
                  <div className="space-y-3 flex-1">
                    <div className="flex gap-2">
                      <div className="w-6 h-6 rounded-full bg-secondary/60 shrink-0" />
                      <div className="rounded-lg bg-secondary/40 px-3 py-2 max-w-[70%]">
                        <div className="h-2 bg-foreground/10 rounded w-48 mb-1.5" />
                        <div className="h-2 bg-foreground/10 rounded w-32" />
                      </div>
                    </div>
                    <div className="flex gap-2">
                      <div className="w-6 h-6 rounded-full bg-cyan-500/20 border border-cyan-500/30 shrink-0" />
                      <div className="rounded-lg bg-card/60 border border-border/50 px-3 py-2 max-w-[80%] space-y-2">
                        <div className="h-2 bg-foreground/10 rounded w-56" />
                        <div className="h-2 bg-foreground/10 rounded w-40" />
                        <div className="rounded bg-background/60 border border-border/30 px-2 py-1.5">
                          <div className="flex items-center gap-1.5">
                            <div className="w-2 h-2 rounded-full bg-emerald-400" />
                            <div className="h-1.5 bg-foreground/10 rounded w-16" />
                          </div>
                        </div>
                        <div className="h-2 bg-foreground/10 rounded w-48" />
                      </div>
                    </div>
                    {/* Agent request mock */}
                    <div className="rounded-xl border border-amber-500/25 bg-amber-500/5 px-3 py-2.5">
                      <div className="flex items-center gap-2 mb-1">
                        <div className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
                        <span className="text-[10px] text-amber-400 font-medium">Агент ждёт ответа</span>
                      </div>
                      <div className="h-1.5 bg-foreground/10 rounded w-44" />
                    </div>
                  </div>
                </div>
                {/* Timeline mock */}
                <div className="w-[200px] border-l border-border/40 p-3 space-y-3 hidden md:block">
                  <div className="text-[10px] font-medium text-muted-foreground/60">Таймлайн</div>
                  {['Анализ', 'web_search', 'python', 'Ответ'].map((step, i) => (
                    <div key={i} className="flex items-center gap-2">
                      <div className={`w-1.5 h-1.5 rounded-full ${
                        i < 3 ? 'bg-emerald-400' : 'bg-cyan-400 animate-pulse'
                      }`} />
                      <span className="text-[10px] text-muted-foreground/60">{step}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </motion.div>
        </div>
      </section>

      {/* Features Grid */}
      <section className="max-w-[1200px] mx-auto px-4 sm:px-6 py-20">
        <div className="text-center mb-12">
          <h2 className="text-2xl md:text-3xl font-display font-semibold tracking-tight mb-3">
            Полный контроль над агентом
          </h2>
          <p className="text-muted-foreground max-w-xl mx-auto">
            Всё что нужно разработчику для наблюдения за работой AI-агента и управления им в реальном времени.
          </p>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {features.map((feature, i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0, y: 12 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.4, delay: i * 0.08 }}
              className="rounded-xl border border-border/70 bg-card/40 p-5 hover:bg-card/60 transition-colors duration-200"
            >
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-secondary/60 mb-3">
                <feature.icon className="h-4 w-4 text-foreground/80" />
              </div>
              <h3 className="text-sm font-semibold mb-1.5">{feature.title}</h3>
              <p className="text-xs text-muted-foreground leading-relaxed">{feature.description}</p>
            </motion.div>
          ))}
        </div>
      </section>

      {/* Capabilities */}
      <section className="max-w-[1200px] mx-auto px-4 sm:px-6 py-16">
        <div className="rounded-xl border border-border/70 bg-card/30 p-8 md:p-12">
          <div className="grid md:grid-cols-2 gap-8 items-center">
            <div>
              <h2 className="text-2xl font-display font-semibold tracking-tight mb-3">
                Создан для серьёзной работы
              </h2>
              <p className="text-sm text-muted-foreground leading-relaxed mb-6">
                Umbrella фиксирует каждую деталь выполнения агента — от отдельных вызовов инструментов до накопленного контекста памяти. Агент может запрашивать дополнительный ввод от вас прямо в процессе работы, не прерывая выполнение.
              </p>
              <div className="space-y-2.5">
                {capabilities.map((cap, i) => (
                  <div key={i} className="flex items-center gap-2.5">
                    <div className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-500/15">
                      <Check className="h-3 w-3 text-emerald-400" />
                    </div>
                    <span className="text-sm">{cap}</span>
                  </div>
                ))}
              </div>
            </div>
            {/* Memory graph preview mock */}
            <div className="rounded-xl border border-border/50 bg-background/40 p-6 relative overflow-hidden">
              <div
                className="absolute inset-0 pointer-events-none"
                style={{
                  background: 'radial-gradient(40% 40% at 85% 15%, rgba(34,211,238,0.08) 0%, rgba(34,211,238,0) 70%)'
                }}
              />
              <svg viewBox="0 0 300 200" className="w-full" style={{ filter: 'drop-shadow(0 0 8px rgba(34,211,238,0.15))' }}>
                <line x1="80" y1="60" x2="180" y2="40" stroke="hsl(215 12% 70% / 0.3)" strokeWidth="1" />
                <line x1="80" y1="60" x2="150" y2="120" stroke="hsl(215 12% 70% / 0.3)" strokeWidth="1" />
                <line x1="180" y1="40" x2="240" y2="100" stroke="hsl(215 12% 70% / 0.3)" strokeWidth="1" />
                <line x1="150" y1="120" x2="240" y2="100" stroke="hsl(215 12% 70% / 0.3)" strokeWidth="1" />
                <line x1="150" y1="120" x2="100" y2="170" stroke="hsl(215 12% 70% / 0.3)" strokeWidth="1" />
                <line x1="240" y1="100" x2="220" y2="170" stroke="hsl(215 12% 70% / 0.3)" strokeWidth="1" />
                {[
                  { x: 80, y: 60, r: 16, color: '#22d3ee', label: 'Задача' },
                  { x: 180, y: 40, r: 14, color: '#34d399', label: 'Концепт' },
                  { x: 150, y: 120, r: 18, color: '#22d3ee', label: 'Сущность' },
                  { x: 240, y: 100, r: 12, color: '#f59e0b', label: 'Инструмент' },
                  { x: 100, y: 170, r: 10, color: '#34d399', label: 'Ссылка' },
                  { x: 220, y: 170, r: 14, color: '#22d3ee', label: 'Решение' },
                ].map((node, i) => (
                  <g key={i}>
                    <circle cx={node.x} cy={node.y} r={node.r} fill={`${node.color}20`} stroke={`${node.color}40`} strokeWidth="1" />
                    <circle cx={node.x} cy={node.y} r="3" fill={node.color} opacity="0.7" />
                  </g>
                ))}
              </svg>
            </div>
          </div>
        </div>
      </section>

      {/* How it works */}
      <section className="max-w-[1200px] mx-auto px-4 sm:px-6 py-16">
        <div className="text-center mb-10">
          <h2 className="text-2xl md:text-3xl font-display font-semibold tracking-tight mb-3">
            Как это работает
          </h2>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          {[
            { step: '01', title: 'Отправьте задачу', desc: 'Напишите задачу в чате. Агент получит её через REST API и запустит Ouroboros LLM-движок.' },
            { step: '02', title: 'Агент работает', desc: 'Агент использует инструменты, строит цепочку рассуждений и может запрашивать комментарии или дополнительные права прямо в UI.' },
            { step: '03', title: 'Результат и обучение', desc: 'Результат записывается в память. Агент анализирует свою работу и улучшает подходы к будущим задачам.' },
          ].map((item, i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0, y: 12 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.4, delay: i * 0.1 }}
              className="rounded-xl border border-border/50 bg-card/30 p-6"
            >
              <div className="text-3xl font-bold text-blue-400/20 font-mono mb-3">{item.step}</div>
              <h3 className="text-sm font-semibold mb-2">{item.title}</h3>
              <p className="text-xs text-muted-foreground leading-relaxed">{item.desc}</p>
            </motion.div>
          ))}
        </div>
      </section>

      {/* CTA */}
      <section className="max-w-[1200px] mx-auto px-4 sm:px-6 py-16 text-center">
        <h2 className="text-2xl md:text-3xl font-display font-semibold tracking-tight mb-3">
          Готовы начать?
        </h2>
        <p className="text-muted-foreground mb-6 max-w-md mx-auto">
          Откройте рабочее пространство и начните взаимодействие с Umbrella. Первый запуск займёт меньше минуты.
        </p>
        <Link to="/chat">
          <Button size="lg" className="gap-2 h-11 px-8">
            Открыть рабочее пространство <ArrowRight className="h-4 w-4" />
          </Button>
        </Link>
      </section>

      {/* Footer */}
      <footer className="border-t border-border/50">
        <div className="max-w-[1200px] mx-auto px-4 sm:px-6 py-6 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Zap className="h-3.5 w-3.5 text-cyan-400" />
            <span className="text-xs text-muted-foreground">Umbrella · AI-агент с самоулучшением</span>
          </div>
          <span className="text-xs text-muted-foreground">На базе Ouroboros LLM</span>
        </div>
      </footer>
    </div>
  );
}
