import React, { useState, useEffect, useCallback } from 'react';
import { SlidersHorizontal, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';
import { api } from './api';
import { useBounty } from './Context';
import { Action, Modal, Field, Status, SectionHeading } from './UI';

const usd = (nano) => `$${(Number(nano || 0) / 1e9).toFixed(2)}`;

export const ExecutionControls = () => {
  const { data: ctx } = useBounty();
  const authenticated = !!ctx?.user;
  const [ctrls, setCtrls] = useState(null);
  const [modal, setModal] = useState(false);
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!authenticated) return;
    try { setCtrls(await api('/execution/controls')); } catch (e) { /* non-fatal */ }
  }, [authenticated]);
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t); }, [load]);

  const open = () => {
    if (!ctrls) return;
    setForm({
      autonomous_execution: ctrls.autonomous_execution, autonomous_escalation: ctrls.autonomous_escalation,
      background_discovery: ctrls.background_discovery,
      daily_usd: (ctrls.daily_budget_nano_usd / 1e9).toFixed(2), per_job_usd: (ctrls.per_job_cap_nano_usd / 1e9).toFixed(2), confirm: false,
    });
    setModal(true);
  };

  const save = async () => {
    if (form.autonomous_execution && !ctrls.autonomous_execution && !form.confirm) {
      toast.error('Confirm you accept real provider spend and GitHub writes to enable autonomous execution.'); return;
    }
    setBusy(true);
    try {
      const c = await api('/execution/controls', 'PUT', {
        autonomous_execution: !!form.autonomous_execution, autonomous_escalation: !!form.autonomous_escalation,
        background_discovery: !!form.background_discovery,
        confirm_autonomous: !!form.confirm,
        daily_budget_nano_usd: Math.round(Number(form.daily_usd) * 1e9),
        per_job_cap_nano_usd: Math.round(Number(form.per_job_usd) * 1e9),
      });
      setCtrls(c); setModal(false); toast.success('Execution controls saved.');
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  const d = ctrls?.driver || {};
  const tiles = ctrls ? [
    ['Autonomous execution', <Status id="cc-auto-exec" value={ctrls.autonomous_execution ? 'ENABLED' : 'DISABLED'} />],
    ['Autonomous escalation', <Status id="cc-auto-esc" value={ctrls.autonomous_escalation ? 'ENABLED' : 'DISABLED'} />],
    ['Continuous discovery (24/7)', <Status id="cc-bg-discovery" value={ctrls.background_discovery ? 'ENABLED' : 'DISABLED'} />],
    ['Background driver', <Status id="cc-driver-status" value={d.status || 'STOPPED'} />],
    ['Why idle', <strong data-testid="cc-driver-reason">{d.reason || '—'}</strong>],
    ['Eligible bounties', <strong data-testid="cc-driver-eligible">{d.eligible_count ?? 0}</strong>],
    ['Active job', <strong data-testid="cc-driver-active">{d.active_job || '—'}</strong>],
    ['Daily budget', <strong data-testid="cc-budget-daily">{usd(ctrls.daily_budget_nano_usd)}</strong>],
    ['Spent today', <strong data-testid="cc-budget-spent">{usd(ctrls.spent_today_nano_usd)}</strong>],
    ['Remaining today', <strong data-testid="cc-budget-remaining">{usd(ctrls.remaining_today_nano_usd)}</strong>],
    ['Per-job cap', <strong data-testid="cc-budget-perjob">{usd(ctrls.per_job_cap_nano_usd)}</strong>],
    ['Reserved today', <strong data-testid="cc-budget-reserved">{usd(ctrls.reserved_today_nano_usd)}</strong>],
    ['Max concurrent', <strong data-testid="cc-budget-concurrent">{ctrls.max_concurrent_jobs}</strong>],
  ] : [];

  return <section className="exec-controls-section" data-testid="cc-panel-autonomous">
    <SectionHeading title="Autonomous execution" detail="Owner-controlled background bounty driver — real spend, backend-gated, never merges">
      <Action id="cc-open-controls" icon={SlidersHorizontal} onClick={open} disabled={!authenticated}>Execution controls</Action>
    </SectionHeading>
    {!authenticated
      ? <div className="auth-required-placeholder" data-testid="cc-controls-auth-required"><ShieldCheck size={22}/><strong>Sign in to access execution controls</strong><span>Autonomous execution, budget and discovery settings require the instance owner session.</span></div>
      : ctrls
      ? <div className="exec-controls-grid">
          {tiles.map(([label, node]) => <div className="exec-tile" key={label}><span>{label}</span>{node}</div>)}
        </div>
      : <p className="exec-controls-loading" data-testid="cc-controls-loading">Loading execution controls…</p>}
    {d.last_error && <p className="exec-controls-note" data-testid="cc-driver-error">Last stop/error: {d.last_error}</p>}

    <Modal open={modal} onClose={() => setModal(false)} title="Execution controls"
      detail="Owner-only. Enabling autonomous execution causes REAL provider spend and REAL GitHub draft-PR writes. Backend gates are never bypassed; at most one job runs at a time and it never merges."
      id="cc-controls-modal">
      {form && <>
        <label className="check-field" htmlFor="cc-cform-exec"><input id="cc-cform-exec" data-testid="cc-cform-exec" type="checkbox" checked={!!form.autonomous_execution} onChange={e => setForm({ ...form, autonomous_execution: e.target.checked })} />Autonomous execution</label>
        <label className="check-field" htmlFor="cc-cform-esc"><input id="cc-cform-esc" data-testid="cc-cform-esc" type="checkbox" checked={!!form.autonomous_escalation} onChange={e => setForm({ ...form, autonomous_escalation: e.target.checked })} />Autonomous model escalation</label>
        <label className="check-field" htmlFor="cc-cform-bgd"><input id="cc-cform-bgd" data-testid="cc-cform-bgd" type="checkbox" checked={!!form.background_discovery} onChange={e => setForm({ ...form, background_discovery: e.target.checked })} />Continuous discovery — keep radar scanning all sources 24/7 (read-only, no spend)</label>
        <div className="form-grid" style={{ marginTop: 16 }}>
          <Field label="Daily budget (USD)" id="cc-cform-daily" type="number" min={0} step="0.01" value={form.daily_usd} onChange={e => setForm({ ...form, daily_usd: e.target.value })} />
          <Field label="Per-job hard cap (USD)" id="cc-cform-perjob" type="number" min={0} step="0.01" value={form.per_job_usd} onChange={e => setForm({ ...form, per_job_usd: e.target.value })} />
        </div>
        {form.autonomous_execution && !ctrls.autonomous_execution && <label className="check-field" htmlFor="cc-cform-confirm" style={{ marginTop: 12 }}><input id="cc-cform-confirm" data-testid="cc-cform-confirm" type="checkbox" checked={!!form.confirm} onChange={e => setForm({ ...form, confirm: e.target.checked })} />I understand this spends real funds and writes real draft PRs to third-party repositories.</label>}
      </>}
      <div className="modal-actions">
        <Action id="cc-cform-cancel" onClick={() => setModal(false)}>Cancel</Action>
        <Action id="cc-cform-save" primary busy={busy} onClick={save}>Save controls</Action>
      </div>
    </Modal>
  </section>;
};
