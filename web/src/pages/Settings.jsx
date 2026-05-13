import React, { useState, useEffect } from 'react';
import { useWorkspace } from '../context/WorkspaceContext';
import { getSettings, updateSettings } from '../lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '../components/ui/card';
import { Input } from '../components/ui/input';
import { Button } from '../components/ui/button';
import { Loader2 } from 'lucide-react';

export default function Settings() {
  const { activeWorkspace } = useWorkspace();
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [maxVerifyRetries, setMaxVerifyRetries] = useState('20');

  useEffect(() => {
    if (!activeWorkspace) return;
    setLoading(true);
    getSettings(activeWorkspace.id)
      .then((data) => {
        setSettings(data);
        setMaxVerifyRetries(String(data.max_verify_retries ?? 20));
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [activeWorkspace]);

  const saveVerificationSettings = async () => {
    if (!activeWorkspace) return;
    setSaving(true);
    try {
      const next = await updateSettings(activeWorkspace.id, {
        max_verify_retries: Number(maxVerifyRetries),
      });
      setSettings(next);
      setMaxVerifyRetries(String(next.max_verify_retries ?? 20));
    } catch (err) {
      console.error(err);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[calc(100vh-56px)]">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!settings) return null;

  return (
    <div data-testid="settings-form" className="px-4 lg:px-8 py-6 max-w-2xl">
      <div className="mb-6">
        <h2 className="text-xl font-display font-semibold tracking-tight">Settings</h2>
        <p className="text-sm text-muted-foreground">Просмотр настроек воркспейса</p>
      </div>

      <div className="space-y-6">
        <Card className="bg-card/60 border-border/70">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-display">Модель LLM</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <p className="text-xs text-muted-foreground">
              Берётся из корневого <code className="text-foreground/90">.env</code> репозитория (
              <code className="text-foreground/90">OUROBOROS_MODEL</code> или{' '}
              <code className="text-foreground/90">LLM_MODEL</code>): значение в файле важнее, чем та же
              переменная, случайно оставленная в окружении shell. Через UI не меняется.
            </p>
            <p className="text-sm font-mono text-foreground" data-testid="settings-env-model-hint">
              {settings.default_model}
            </p>
          </CardContent>
        </Card>

        <Card className="bg-card/60 border-border/70">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-display">Verification remediation attempts</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-xs text-muted-foreground">
              Focused repair attempts inside the same run after failed verification. The bridge default is{' '}
              <code className="text-foreground/90">20</code> and can also be set before launch via{' '}
              <code className="text-foreground/90">OUROBOROS_WEB_MAX_VERIFY_RETRIES</code>.
            </p>
            <div className="flex items-center gap-2">
              <Input
                type="number"
                min="0"
                step="1"
                value={maxVerifyRetries}
                onChange={(event) => setMaxVerifyRetries(event.target.value)}
                className="w-28"
                data-testid="settings-max-verify-retries-input"
              />
              <Button
                type="button"
                onClick={saveVerificationSettings}
                disabled={saving}
                data-testid="settings-save-verification-button"
              >
                {saving ? 'Saving...' : 'Save'}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
