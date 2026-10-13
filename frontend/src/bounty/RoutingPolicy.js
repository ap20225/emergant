import React, { useEffect, useState } from 'react';
import { Save, RefreshCw } from 'lucide-react';
import { useBounty } from './Context';
import { api, when } from './api';
import { Action, Field, SectionHeading, Status } from './UI';

export const RoutingPolicy = () => {
  const { data } = useBounty();
  const [view, setView] = useState(null), [draft, setDraft] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [saved, setSaved] = useState(false);
  const load = async () => { const value = await api('/routing-policy'); setView(value); setDraft({ ...value.policy, ...Object.fromEntries(['routine', 'coding', 'review', 'paid_escalation'].map(k => [k, value.policy[k].join('\n')])) }); };
  useEffect(() => { if (!data.preview) load().catch(e => setError(e.message)); }, [data.preview]);
  const change = (key, value) => { setDraft(v => ({ ...v, [key]: value })); setSaved(false); };
  const save = async () => {
    setBusy(true); setError(''); setSaved(false);
    try { const payload = { ...draft, ...Object.fromEntries(['routine', 'coding', 'review', 'paid_escalation'].map(k => [k, draft[k].split(/[\n,]+/).map(s => s.trim()).filter(Boolean)])) }; await api('/routing-policy', 'PUT', payload); await load(); setSaved(true); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  };
  if (data.preview) return <p data-testid="routing-owner-required">Owner sign-in required to view or edit model policy.</p>;
  return <section data-testid="free-first-policy">
    <SectionHeading title="Free-first model policy" detail="Role preferences apply immediately after saving. Paid execution remains disabled."/>
    {error && <p role="alert" className="findings warning" data-testid="routing-policy-error">{error}</p>}
    {!draft ? <p data-testid="routing-policy-loading">Loading policy…</p> : <>
      <div className="connection-facts"><Status id="routing-strategy" value="FREE FIRST"/><Status id="routing-free-status" value={view.free_status}/><span data-testid="routing-free-count">{view.verified_free_count} verified-free models / {view.catalog_model_count} catalog models</span></div>
      <p className="findings warning" data-testid="routing-free-reason">{view.free_reason}</p>
      <div className="settings-fields">{[['routine', 'Triage / research / simple questions'], ['coding', 'First implementation attempt'], ['review', 'Tests / review'], ['paid_escalation', 'Paid escalation preferences · DISABLED']].map(([key, title]) => <Field key={key} id={`routing-${key.replaceAll('_', '-')}`} label={title}><textarea id={`routing-${key.replaceAll('_', '-')}`} data-testid={`routing-${key.replaceAll('_', '-')}`} rows={5} maxLength={3000} value={draft[key]} onChange={e => change(key, e.target.value)}/>{view.groups[key] && <small data-testid={`routing-${key}-available`}>Catalog matches: {view.groups[key].available_preferences.join(', ') || 'None'}. Verified-free route: {view.groups[key].verified_free_order.join(' → ') || 'BLOCKED'}.</small>}</Field>)}</div>
      <Field id="routing-max-attempts" label="Maximum distinct free attempts per task · 1–2" type="number" min={1} max={2} value={draft.max_free_attempts} onChange={e => change('max_free_attempts', Number(e.target.value))}/>
      <div className="connection-facts">{[['invalid_patch', 'Invalid patch'], ['tests_failed', 'Failed tests'], ['review_rejected', 'Rejected review']].map(([key, name]) => <label key={key}><input data-testid={`routing-trigger-${key.replaceAll('_', '-')}`} type="checkbox" checked={draft.escalation_triggers.includes(key)} onChange={e => change('escalation_triggers', e.target.checked ? [...draft.escalation_triggers, key] : draft.escalation_triggers.filter(v => v !== key))}/>{name}</label>)}</div>
      <p className="muted" data-testid="routing-paid-lock">$0 ongoing authorization · paid execution disabled · $1 diagnostic ceiling unverified. A failed free attempt does not authorize spending. Security/policy failures and ambiguous charges stop routing.</p>
      <div className="connection-actions"><Action type="button" id="save-routing-policy" icon={Save} primary busy={busy} disabled={busy} onClick={save}>Save routing policy</Action><Action type="button" id="reload-routing-policy" icon={RefreshCw} disabled={busy} onClick={() => load().catch(e => setError(e.message))}>Reload saved policy</Action>{saved && <span role="status" data-testid="routing-policy-saved">Policy saved. Spending limits unchanged.</span>}</div>
      <SectionHeading title="Routing decisions" detail="Unknown cost and token counts are never presented as zero."/>
      {!view.decisions.length ? <p className="muted" data-testid="routing-decisions-empty">No routing attempts recorded.</p> : <div className="routing-decisions">{view.decisions.map(d => <div key={d.id} data-testid={`routing-decision-${d.id}`}><strong>{d.role} · {d.model || 'No eligible model'}</strong><p>{d.result} · reason: {d.escalation_reason || 'initial selection'} · {when(d.created_at)}</p><small>Estimated cost: {d.estimated_cost_nano_usd == null ? 'Unknown' : `$${(d.estimated_cost_nano_usd / 1e9).toFixed(9)}`} · tokens: {d.prompt_tokens ?? 'Unknown'} / {d.completion_tokens ?? 'Unknown'} · paid: DISABLED</small></div>)}</div>}
    </>}
  </section>;
};