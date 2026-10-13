import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Github, RefreshCw, ShieldCheck, Unplug } from 'lucide-react';
import { toast } from 'sonner';
import { useBounty } from './Context';
import { api, when } from './api';
import { Action, Field, PageHeading, SectionHeading, Status } from './UI';

export default function SecurityConnections() {
  const { data, refresh, control } = useBounty();
  const [state, setState] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState('');
  const [github, setGithub] = useState({ repository: '', issue_number: '', token: '' });
  const [runner, setRunner] = useState({ api_key: '', template_id: '' });
  const [suite, setSuite] = useState('pytest');
  const load = async () => setState(await api('/execution/status'));
  useEffect(() => { if (!data.preview) load().catch(e => setError(e.message)); }, [data.preview]);
  const run = async (id, action) => {
    setBusy(id); setError('');
    try { await action(); await load(); await refresh(); }
    catch (e) { setError(e.message); }
    finally { setBusy(''); setGithub(v => ({ ...v, token: '' })); setRunner(v => ({ ...v, api_key: '' })); }
  };
  const disconnect = name => run(`disconnect-${name}`, () => api(`/connections/${name}`, 'DELETE'));
  if (data.preview) return <><PageHeading title="Security & connections"/><div className="information-band" data-testid="connections-owner-required"><ShieldCheck size={18}/><Link to="/login" data-testid="connections-sign-in">Sign in as the instance owner to configure connections.</Link></div></>;
  const model = state?.model, connected = state?.github?.status === 'CONNECTED';
  return <>
    <PageHeading title="Security & connections" eyebrow="REAL EXECUTION" description="One authorized repository. Server-side credentials. Human-approved publication.">
      <Status id="real-execution-status" value={state?.status || 'CHECKING'}/>
      <Action id="refresh-execution-status" icon={RefreshCw} busy={busy === 'refresh'} onClick={() => run('refresh', load)}>Refresh status</Action>
    </PageHeading>
    {error && <div className="findings warning" role="alert" data-testid="connection-error">{error}</div>}
    <section className="connection-section">
      <SectionHeading title="GitHub" detail="Fine-grained token: one public repository you own. Start with metadata, contents and issues read-only."/>
      <Status id="integration-status-github" value={state?.github?.status || 'NOT CONNECTED'}/>
      {connected && <p className="muted" data-testid="github-verified-details">{state.github.login} · {state.github.repository} · issue #{state.github.issue_number} · read-verified {when(state.github.verified_at)}. Write access is not verified.</p>}
      <form className="connection-form" data-testid="github-connect-form" onSubmit={e => { e.preventDefault(); run('github', async () => { await api('/connections/github', 'POST', { ...github, issue_number: Number(github.issue_number) }); toast.success('GitHub identity, repository and issue reads verified.'); }); }}>
        <Field id="github-repository" label="Public repository · owner/name" required maxLength={180} value={github.repository} onChange={e => setGithub({ ...github, repository: e.target.value })}/>
        <Field id="github-issue-number" label="Open issue number" type="number" required min={1} value={github.issue_number} onChange={e => setGithub({ ...github, issue_number: e.target.value })}/>
        <Field id="github-token" label="Fine-grained token" type="password" autoComplete="new-password" required minLength={20} maxLength={300} value={github.token} onChange={e => setGithub({ ...github, token: e.target.value })}/>
        <div className="connection-actions"><Action type="submit" id="connect-github" icon={Github} primary busy={busy === 'github'} disabled={!!busy}>Connect GitHub</Action>
          <Action type="button" id="verify-github-connection" disabled={!state?.github?.repository || !!busy} onClick={() => run('verify-github', () => api('/connections/github/verify', 'POST'))}>Verify GitHub</Action>
          <Action type="button" id="disconnect-github" icon={Unplug} disabled={!state?.github?.repository || !!busy} onClick={() => disconnect('github')}>Disconnect</Action></div>
      </form>
    </section>
    <section className="connection-section">
      <SectionHeading title="Experiential Labs" detail="Stored server-side credential. Catalog access is separate from verified inference."/>
      <div className="connection-facts"><Status id="integration-status-model" value={model?.catalog_status || 'NOT CHECKED'}/><span data-testid="model-key-status">Credential: {model?.credential_configured ? 'CONFIGURED' : 'NOT CONFIGURED'}</span><span data-testid="model-catalog-count">{model?.model_count || 0} catalog models</span><span data-testid="model-inference-status">Inference: {model?.inference_verified_at ? `verified ${when(model.inference_verified_at)}` : 'NOT VERIFIED'}</span></div>
      <div className="connection-actions"><Action id="verify-model-catalog" busy={busy === 'models'} disabled={!!busy} onClick={() => run('models', () => api('/connections/model/catalog', 'POST'))}>Verify catalog · no inference</Action><Action id="model-diagnostic-check" disabled={!!busy || !model?.model_count} onClick={() => run('diagnostic', () => api('/connections/model/diagnostic', 'POST'))}>Check one-time diagnostic gate</Action></div>
      <p className="findings warning" data-testid="model-diagnostic-blocker">{model?.diagnostic_reason}</p>
      <div className="connection-facts"><span data-testid="diagnostic-allowance-status">$1 maximum · {state?.diagnostic_attempted ? 'attempt already consumed' : 'no attempt recorded'} · no retries</span><span data-testid="ongoing-spend-status">Ongoing spending: $0 authorized</span></div>
      <p><Link to="/settings?tab=models" data-testid="edit-free-first-routing">Edit free-first model policy →</Link></p>
      <p className="muted" data-testid="model-pricing-classification">Catalog pricing is estimated, not a billing receipt. Missing token or billing data stays unknown.</p>
      {state?.usage?.length ? <div className="table-container"><table className="data-table" data-testid="model-usage-table"><thead><tr><th>MODEL / ROLE</th><th>TOKENS IN / OUT</th><th>LATENCY</th><th>ACTUAL COST</th><th>STATUS</th></tr></thead><tbody>{state.usage.map(u => <tr key={u.id} data-testid={`model-usage-${u.id}`}><td>{u.model}<small>{u.role}</small></td><td>{u.prompt_tokens ?? 'Unknown'} / {u.completion_tokens ?? 'Unknown'}</td><td>{u.latency_ms == null ? 'Unknown' : `${u.latency_ms} ms`}</td><td>{u.actual_cost_nano_usd == null ? 'Pending / unknown' : `$${(u.actual_cost_nano_usd / 1e9).toFixed(9)}`}</td><td>{u.status} · {u.cost_status}</td></tr>)}</tbody></table></div> : <p className="muted" data-testid="model-usage-empty">No inference attempted. Tokens, latency and actual inference cost are not available.</p>}
    </section>
    <section className="connection-section">
      <SectionHeading title="Isolated runner · E2B" detail="Remote sandbox only. No repository execution on the application host. Configuration does not authorize charges."/>
      <Status id="integration-status-production_sandbox" value={state?.runner?.status || 'BLOCKED · RUNNER_REQUIRED'}/>
      {state?.runner?.template_id && <p className="muted" data-testid="runner-saved-details">Saved template: <strong data-testid="runner-saved-template">{state.runner.template_id}</strong> · credential {state.runner.credential_configured ? 'stored securely' : 'missing'} · configured {when(state.runner.configured_at)} · runtime {state.runner.verified_at ? `verified ${when(state.runner.verified_at)}` : 'not yet verified'}</p>}
      <form className="connection-form" data-testid="runner-connect-form" onSubmit={e => { e.preventDefault(); run('runner', () => api('/connections/runner', 'POST', runner)); }}>
        <Field id="runner-template-id" label="Dependency-ready E2B template ID" required pattern="[a-zA-Z0-9_-]{1,100}" value={runner.template_id} onChange={e => setRunner({ ...runner, template_id: e.target.value })}/>
        <Field id="runner-api-key" label="E2B API key" type="password" autoComplete="new-password" required minLength={16} maxLength={300} value={runner.api_key} onChange={e => setRunner({ ...runner, api_key: e.target.value })}/>
        <div className="connection-actions"><Action type="submit" id="configure-runner" busy={busy === 'runner'} disabled={!!busy}>Save runner configuration</Action><Action type="button" id="disconnect-runner" disabled={!state?.runner?.credential_configured || !!busy} onClick={() => disconnect('runner')}>Disconnect runner</Action></div>
      </form>
      <p className="muted" data-testid="runner-setup-required">Required: E2B account, sandbox template with Git and your test dependencies, non-root user, network controls, and a separately approved bounded runtime budget. No sandbox has been created by this setup.</p>
    </section>
    <section className="connection-section"><SectionHeading title="Controlled real issue" detail="Only the connected issue in your own repository. No reward or payment assumed."/>
      <Field id="controlled-test-suite" label="Repository test suite"><select data-testid="controlled-test-suite" id="controlled-test-suite" value={suite} onChange={e => setSuite(e.target.value)}>{['pytest', 'unittest', 'jest', 'vitest', 'go'].map(v => <option key={v}>{v}</option>)}</select></Field>
      <Action id="start-controlled-job" disabled={!connected || !!busy} onClick={() => run('start', () => api('/execution/start', 'POST', { issue_number: state.github.issue_number, test_suite: suite, confirm_execution: true }))}>Request controlled execution</Action>
      <ul className="execution-blockers" data-testid="execution-blockers">{state?.blockers?.map(b => <li key={b.code} data-testid={`blocker-${b.code.toLowerCase().replaceAll('_', '-')}`}><strong>{b.code}</strong><p>{b.action}</p></li>)}</ul>
    </section>
    <section className="connection-section"><SectionHeading title="Emergency controls" detail="ASSISTED · no automatic merges · no wallet spending"/><div className="emergency-grid">{[['STOP_AGENTS', 'Stop all agents'], ['STOP_SPENDING', 'Stop spending'], ['DISABLE_SUBMISSIONS', 'Disable submissions'], ['DISABLE_NEW_JOBS', 'Disable new jobs'], ['REVOKE_INTEGRATIONS', 'Revoke integration access'], ['RESUME', 'Resume operations']].map(([id, name]) => <Action key={id} id={`security-${id.toLowerCase()}`} danger={id !== 'RESUME'} onClick={async () => { await control(id); await load(); }}>{name}</Action>)}</div><p className="muted" data-testid="integration-revocation-note">Revocation blocks app access; provider-side revocation remains separate. Resume does not restore revoked integration access.</p></section>
  </>;
}