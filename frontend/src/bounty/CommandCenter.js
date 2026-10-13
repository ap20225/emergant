import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Activity, AlertTriangle, Bot, CheckCircle2, Clock, Compass, Cpu, Database, DollarSign, FileText, GitPullRequest, PlayCircle, RefreshCw, Radar, ShieldAlert } from 'lucide-react';
import { toast } from 'sonner';
import { api, attempt, when } from './api';
import { Action, Empty, Field, Modal, PageHeading, Status } from './UI';

const nano = (n, d = 6) => (typeof n !== 'number') ? '—' : '$' + (n / 1e9).toFixed(d);
const usd = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n || 0);
const slug = (s = '') => String(s).toLowerCase().replaceAll(/[^a-z0-9]/g, '-');
const isNum = (v) => typeof v === 'number';

const Panel = ({ title, icon: Icon, right, children, id }) => (
  <section className="cc-panel" data-testid={`cc-panel-${id}`}>
    <div className="cc-panel-head"><span className="cc-panel-title">{Icon && <Icon size={16} />}{title}</span>{right}</div>
    {children}
  </section>
);

const Cap = ({ name, value }) => (
  <div className="cc-cap" data-testid={`cc-cap-${slug(name)}`}>
    <span>{name}</span><Status id={`cc-cap-status-${slug(name)}`} value={value || 'UNKNOWN'} />
  </div>
);

const Count = ({ label, value, tone }) => (
  <div className={`cc-count ${tone || ''}`} data-testid={`cc-count-${slug(label)}`}>
    <strong>{value}</strong><span>{label.replaceAll('_', ' ')}</span>
  </div>
);

const KV = ({ k, children, testid }) => (
  <div className="cc-kv" data-testid={testid}><span>{k}</span><strong>{children}</strong></div>
);

// A value that is either a real count (show number, even 0) or NOT_TRACKED (honest empty state)
const Metric = ({ k, value, testid }) => (
  <KV k={k} testid={testid}>{value === 'NOT_TRACKED' ? <span className="nt">NOT TRACKED</span> : value}</KV>
);

export default function CommandCenter() {
  const [cc, setCc] = useState(null), [err, setErr] = useState(''), [busy, setBusy] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [cfgModal, setCfgModal] = useState(false), [briefModal, setBriefModal] = useState(false);
  const [brief, setBrief] = useState(null), [cfg, setCfg] = useState({ query: '', per_page: 20, enabled: false });
  const [jobId, setJobId] = useState(null), [job, setJob] = useState(null);
  const [runConfirm, setRunConfirm] = useState(null), [stopConfirm, setStopConfirm] = useState(false), [stopping, setStopping] = useState(false);
  const [polModal, setPolModal] = useState(false), [pol, setPol] = useState(null);
  const [ctrls, setCtrls] = useState(null), [ctrlModal, setCtrlModal] = useState(false), [cform, setCform] = useState(null);

  const load = useCallback(async (manual) => {
    if (manual) setRefreshing(true);
    try { const d = await api('/command-center'); setCc(d); setErr(''); }
    catch (e) { setErr(e.message); }
    try { const c = await api('/execution/controls'); setCtrls(c); } catch (e) { /* controls are non-fatal to the dashboard */ }
    finally { if (manual) setTimeout(() => setRefreshing(false), 350); }
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t); }, [load]);

  // Live job polling: every 2s until the backend reports a terminal state (active === false).
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false, timer;
    const tick = async () => {
      try {
        const s = await api(`/execution/bounty/${jobId}/state`);
        if (cancelled) return;
        setJob(s);
        if (s.active === false) { setStopping(false); return; }
        timer = setTimeout(tick, 2000);
      } catch (e) { if (!cancelled) toast.error(e.message); }
    };
    timer = setTimeout(tick, 0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [jobId]);

  const run = async (id, fn, done) => {
    setBusy(id);
    await attempt(async () => { const r = await fn(); await load(); done?.(r); });
    setBusy('');
  };
  const openConfig = () => { setCfg(cc.system.discovery_config); setCfgModal(true); };
  const saveConfig = () => run('save-config', () => api('/discovery/config', 'PUT', {
    query: cfg.query, per_page: Number(cfg.per_page), enabled: !!cfg.enabled,
  }), () => { setCfgModal(false); toast.success('Discovery config saved. No execution authorized.'); });
  const runDiscovery = () => run('run-discovery', () => api('/discovery/github/run', 'POST'),
    (r) => toast.success(`Discovery: ${r.created ?? 0} new, ${r.duplicates ?? 0} duplicate. Read-only search.`));
  const runScheduler = () => run('run-scheduler', () => api('/scheduler/run', 'POST'),
    (r) => toast.success(r.skipped ? `Scheduler skipped (${r.skipped})` : `Scheduler cycle: ${r.discovered} discovered, ${r.qualified} qualified. No jobs executed.`));
  const genBriefing = () => run('gen-briefing', () => api('/briefing/run', 'POST'),
    () => toast.success('Deterministic briefing generated. No model spend.'));
  const viewBriefing = () => run('view-briefing', () => api('/briefing/latest'),
    (r) => { setBrief(r); setBriefModal(true); });
  const qualify = (id) => run(`qualify-${id}`, () => api(`/opportunities/${id}/qualify`, 'POST'),
    (r) => toast.success(`Qualified → ${r.status}. Human authorization still required.`));
  const recheck = (id) => run(`recheck-${id}`, () => api(`/opportunities/${id}/recheck`, 'POST'),
    (r) => toast[r.fresh ? 'success' : 'warning'](r.fresh ? 'Source state fresh.' : `Stale: ${r.reason}`));
  const doRun = () => run(`run-bounty-${runConfirm.id}`, () => api('/execution/bounty/run', 'POST', { opportunity_id: runConfirm.id, confirm: true, owner_approved: true, acknowledge_unverified_payout: true }),
    (r) => { setRunConfirm(null); setJob({ job_id: r.job_id, stage: r.stage || r.status, active: true }); setJobId(r.job_id); toast.success(`Bounty run queued · ${r.job_id}. Draft PR only — no merge.`); });
  const doStop = async () => {
    setStopConfirm(false); setStopping(true);
    try { await api(`/execution/bounty/${jobId}/stop`, 'POST'); toast.success('Stop requested — cancelling attempts and terminating sandbox.'); }
    catch (e) { toast.error(e.message); setStopping(false); }
  };
  const openPolicy = () => run('open-policy', () => api('/execution/policy'), (r) => { setPol(r); setPolModal(true); });
  const savePolicy = () => run('save-policy-exec', () => api('/execution/policy', 'PUT', {
    max_total_model_attempts: Number(pol.max_total_model_attempts), max_attempts_per_model: Number(pol.max_attempts_per_model),
    max_corrections_per_model: Number(pol.max_corrections_per_model), max_total_corrections: Number(pol.max_total_corrections),
    max_timeout_retries_per_model: Number(pol.max_timeout_retries_per_model), max_model_escalations: Number(pol.max_model_escalations),
  }), (r) => { setPol(r); setPolModal(false); toast.success('Execution policy saved. Budget cap unchanged.'); });
  const openControls = () => run('open-controls', () => api('/execution/controls'), (c) => {
    setCtrls(c);
    setCform({ autonomous_execution: c.autonomous_execution, autonomous_escalation: c.autonomous_escalation,
      daily_usd: (c.daily_budget_nano_usd / 1e9).toFixed(2), per_job_usd: (c.per_job_cap_nano_usd / 1e9).toFixed(2), confirm: false });
    setCtrlModal(true);
  });
  const saveControls = () => {
    if (cform.autonomous_execution && !ctrls?.autonomous_execution && !cform.confirm) {
      toast.error('Confirm you accept real provider spend and GitHub writes to enable autonomous execution.'); return;
    }
    run('save-controls', () => api('/execution/controls', 'PUT', {
      autonomous_execution: !!cform.autonomous_execution, autonomous_escalation: !!cform.autonomous_escalation,
      confirm_autonomous: !!cform.confirm,
      daily_budget_nano_usd: Math.round(Number(cform.daily_usd) * 1e9),
      per_job_cap_nano_usd: Math.round(Number(cform.per_job_usd) * 1e9),
    }), (c) => { setCtrls(c); setCtrlModal(false); toast.success('Execution controls saved.'); load(); });
  };

  if (err && !cc) return (
    <><PageHeading eyebrow="AUTONOMY / OBSERVABILITY" title="Command Center" />
      <div className="system-banner error" data-testid="cc-data-unavailable" style={{ borderRadius: 6 }}>
        <Activity size={15} />{err} <button data-testid="cc-retry" onClick={load}>Retry</button></div></>
  );
  if (!cc) return <Empty id="cc-loading" icon={Radar} title="Loading command center…" detail="Reading live system, source, opportunity, finance, execution and safety state." />;

  const { system: sys, sources, opportunities: opps, finance: fin, execution: exe, safety: saf } = cc;
  const cap = sys.capability_matrix, flags = sys.feature_flags, sc = sys.scheduler;
  const strip = [
    ['SYSTEM', sys.status], ['DISCOVERY', cap.DISCOVERY], ['QUALIFICATION', cap.QUALIFICATION],
    ['SCHEDULER', sc.background_enabled ? 'READY' : 'PARTIAL'],
    ['EXECUTION', flags.AUTONOMOUS_EXECUTION_ENABLED ? 'ENABLED' : 'DISABLED'],
    ['BUDGET', fin.run_budget.authorized ? 'AUTHORIZED' : 'NOT_AUTHORIZED'],
    ['SAFETY', (saf.prompt_injection_rejects || saf.unsafe_patch_rejects || saf.blocked_publications) ? 'PARTIAL' : 'READY'],
  ];

  // What needs my attention (only truthful, data-derived signals)
  const attn = [];
  if (opps.counts.AWAITING_AUTHORIZATION) attn.push(['Opportunities awaiting authorization', opps.counts.AWAITING_AUTHORIZATION, '/opportunities']);
  if (exe.awaiting_approval) attn.push(['Jobs awaiting approval', exe.awaiting_approval, '/pull-requests']);
  if (exe.counts.NEEDS_REVIEW) attn.push(['Jobs needing review', exe.counts.NEEDS_REVIEW, '/jobs']);
  if (opps.counts.STALE_REQUIRES_RECHECK) attn.push(['Stale opportunities', opps.counts.STALE_REQUIRES_RECHECK, '/opportunities']);
  const badSources = sources.filter(s => s.status !== 'HEALTHY' || (s.consecutive_failures || 0) > 0).length;
  if (badSources) attn.push(['Source failures', badSources, null]);
  if (saf.prompt_injection_rejects || saf.unsafe_patch_rejects || saf.blocked_publications)
    attn.push(['Safety events recorded', saf.prompt_injection_rejects + saf.unsafe_patch_rejects + saf.blocked_publications, '/security']);

  const oppTone = { DISCOVERED: '', QUALIFIED: 'good', AWAITING_AUTHORIZATION: 'warn', STALE_REQUIRES_RECHECK: 'warn', REJECTED: 'bad', COMPLETED: 'good' };
  const terminal = !!job && job.active === false;
  const canRun = (s) => ['QUALIFIED', 'AWAITING_AUTHORIZATION'].includes(s);

  return <>
    <PageHeading eyebrow="AUTONOMY / OBSERVABILITY" title="Command Center" description="Live system, sources, opportunities, finance, execution and safety. Read-only observability — discovery and qualification never authorize execution.">
      <Action id="cc-refresh" icon={RefreshCw} busy={refreshing} onClick={() => load(true)}>Refresh</Action>
    </PageHeading>

    <div className="cc-updated" data-testid="cc-last-updated"><Clock size={12} /> Last updated {new Date(cc.generated_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })}</div>

    <div className="cc-strip" data-testid="cc-global-strip">
      {strip.map(([name, val]) => <div className="cc-strip-item" key={name} data-testid={`cc-strip-${slug(name)}`}>
        <span>{name}</span><Status id={`cc-strip-status-${slug(name)}`} value={val} /></div>)}
    </div>

    <div className={`cc-attention ${attn.length ? '' : 'clear'}`} data-testid="cc-attention">
      <div className="cc-attention-head">{attn.length ? <AlertTriangle size={16} /> : <CheckCircle2 size={16} />}{attn.length ? 'What needs your attention' : 'All systems clear'}</div>
      {attn.length ? <div className="cc-attention-list">{attn.map(([t, n, to]) => to
        ? <Link key={t} to={to} data-testid={`cc-attn-${slug(t)}`}>{t}<b>{n}</b></Link>
        : <span key={t} data-testid={`cc-attn-${slug(t)}`}>{t}<b>{n}</b></span>)}</div>
        : <p className="muted" style={{ fontSize: 11 }}>No opportunities awaiting authorization, no jobs needing review, no source failures and no safety events recorded.</p>}
    </div>

    <div className="cc-grid">
      {jobId && job && <section className="cc-panel cc-job-live" data-testid="cc-live-job" style={{ gridColumn: '1 / -1' }}>
        <div className="cc-panel-head">
          <span className="cc-panel-title"><Activity size={16} />Live job · {job.job_id}</span>
          <div className="cc-controls" style={{ margin: 0 }}>
            {!terminal && <Action id="cc-job-stop" danger icon={ShieldAlert} busy={stopping} onClick={() => setStopConfirm(true)}>{stopping ? 'Stopping…' : 'Stop job'}</Action>}
            <Action id="cc-job-close" onClick={() => { setJobId(null); setJob(null); setStopping(false); }}>Close</Action>
          </div>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))', gap: '2px 28px' }}>
          <KV k="Stage" testid="cc-job-stage"><Status id="cc-job-stage-status" value={job.stage || 'UNKNOWN'} /></KV>
          <KV k="Bounty" testid="cc-job-bounty">{job.bounty_id || '—'}</KV>
          <KV k="Repo # issue" testid="cc-job-repo">{job.repository ? `${job.repository}#${job.issue_number ?? '?'}` : '—'}</KV>
          <KV k="Reward" testid="cc-job-reward">{isNum(job.reward) && job.reward ? usd(job.reward) : <span className="nt">REWARD UNKNOWN</span>}</KV>
          <KV k="Model" testid="cc-job-model">{job.model || '—'}</KV>
          <KV k="Attempt" testid="cc-job-attempt">{job.attempt ?? '—'}{job.max_attempts ? ` / ${job.max_attempts}` : ''}</KV>
          <KV k="Corrections" testid="cc-job-correction">{job.correction ?? 0}</KV>
          <KV k="Tests" testid="cc-job-test"><Status id="cc-job-test-status" value={job.test_status || 'UNKNOWN'} /></KV>
          <KV k="Security" testid="cc-job-security"><Status id="cc-job-sec-status" value={job.security_status || 'UNKNOWN'} /></KV>
          <KV k="Code review" testid="cc-job-review"><Status id="cc-job-review-status" value={job.review_status || 'UNKNOWN'} /></KV>
          <KV k="Sandbox (E2B)" testid="cc-job-sandbox">{job.sandbox || <span className="nt">NONE</span>}</KV>
          <KV k="Branch" testid="cc-job-branch">{job.branch || '—'}</KV>
          <KV k="Commit" testid="cc-job-commit">{job.commit ? String(job.commit).slice(0, 10) : '—'}</KV>
          <KV k="Provider cost (spent)" testid="cc-job-cost-spent">{nano(job.cost?.spent_nano_usd, 6)}</KV>
          <KV k="Budget cap" testid="cc-job-cost-cap">{nano(job.cost?.cap_nano_usd, 2)}</KV>
          <KV k="Remaining" testid="cc-job-cost-remaining">{nano(job.cost?.remaining_nano_usd, 2)}</KV>
        </div>
        <div style={{ marginTop: 12 }}>
          {job.pr && job.pr.url
            ? <KV k="Draft PR" testid="cc-job-pr"><a href={job.pr.url} target="_blank" rel="noreferrer" className="quiet-link">#{job.pr.number} · {job.pr.status}</a></KV>
            : <KV k="Draft PR" testid="cc-job-pr"><span className="nt">NOT PUBLISHED</span></KV>}
          {job.failure_class && <KV k="Failure" testid="cc-job-failure"><Status id="cc-job-failure-status" value={job.failure_class} />{job.next_action ? ` · ${job.next_action}` : ''}</KV>}
          {job.error && <p className="cc-boundary" data-testid="cc-job-error" style={{ color: 'var(--red)' }}>{job.error}</p>}
        </div>
        <p className="cc-boundary" data-testid="cc-job-boundary">{terminal ? 'Terminal state — polling stopped. Draft PR only; no merge is ever performed.' : 'Polling every 2s. The backend is authoritative; the UI cannot force a state.'}</p>
      </section>}
      {/* SYSTEM */}
      <Panel id="system" title="System" icon={Cpu}>
        <div className="cc-caps">
          <Cap name="Controlled Operation" value={cap.CONTROLLED_OPERATION} />
          <Cap name="Unattended Execution" value={cap.UNATTENDED_EXECUTION} />
          <Cap name="Discovery" value={cap.DISCOVERY} />
          <Cap name="Real Source" value={cap.REAL_SOURCE} />
          <Cap name="Qualification" value={cap.QUALIFICATION} />
          <Cap name="Live Recheck" value={cap.LIVE_RECHECK} />
          <Cap name="Persistent Scheduler" value={cap.PERSISTENT_SCHEDULER} />
          <Cap name="Nightly Briefing" value={cap.NIGHTLY_BRIEFING} />
          <Cap name="Auto Escalation" value={cap.AUTO_ESCALATION} />
        </div>
        <div style={{ marginTop: 14 }}>
          <KV k="Background scheduler" testid="cc-sched-bg"><Status id="cc-sched-bg-status" value={sc.background_enabled ? 'ENABLED' : 'DISABLED'} /></KV>
          <KV k="Scheduler last run" testid="cc-sched-last">{when(sc.last_finished_at)}</KV>
          <KV k="Scheduler tick" testid="cc-sched-tick">{sc.tick_seconds}s</KV>
        </div>
        <div className="cc-controls">
          <Action id="cc-run-scheduler" icon={PlayCircle} busy={busy === 'run-scheduler'} onClick={runScheduler}>Run scheduler once</Action>
          <Action id="cc-gen-briefing" icon={FileText} busy={busy === 'gen-briefing'} onClick={genBriefing}>Generate briefing</Action>
          <Action id="cc-view-briefing" busy={busy === 'view-briefing'} onClick={viewBriefing}>View latest briefing</Action>
        </div>
        <p className="cc-boundary" data-testid="cc-boundary-note">Scheduler runs discovery / qualification / recheck / briefing only. It never executes bounty jobs while unattended execution is disabled.</p>
      </Panel>

      {/* SOURCES */}
      <Panel id="sources" title="Sources" icon={Database} right={
        <div className="cc-controls" style={{ margin: 0 }}>
          <Action id="cc-edit-config" busy={busy === 'save-config'} onClick={openConfig}>Edit config</Action>
          <Action id="cc-run-discovery" icon={Compass} busy={busy === 'run-discovery'} onClick={runDiscovery}>Run discovery</Action>
        </div>}>
        {sources.length ? sources.map(s => <div className="cc-src" key={s.id} data-testid={`cc-source-${slug(s.id)}`}>
          <div className="cc-src-top"><strong>{s.id}</strong><Status id={`cc-source-status-${slug(s.id)}`} value={s.status || 'UNKNOWN'} /></div>
          <div className="cc-src-grid">
            <span>Discovered<b>{s.total_discovered ?? 0}</b></span>
            <span>Duplicates<b>{s.total_duplicates ?? 0}</b></span>
            <span>Consecutive failures<b>{s.consecutive_failures ?? 0}</b></span>
            <span>Last success<b>{when(s.last_success_at)}</b></span>
            <span>Last checked<b>{when(s.last_checked_at)}</b></span>
            <span>Last failure<b>{when(s.last_failure_at)}</b></span>
          </div>
          {s.last_error && <p className="cc-boundary" data-testid={`cc-source-error-${slug(s.id)}`} style={{ color: 'var(--red)' }}>Last error: {s.last_error}</p>}
        </div>) : <Empty id="cc-sources-empty" icon={Database} title="No sources yet" detail="Run discovery to populate GitHub source health. Discovered rewards are shown as UNKNOWN unless the source provides them." />}
        <p className="cc-boundary">Query: <code>{sys.discovery_config.query}</code> · per_page {sys.discovery_config.per_page} · {sys.discovery_config.enabled ? 'scheduled discovery ON' : 'scheduled discovery OFF'}</p>
      </Panel>

      {/* OPPORTUNITIES */}
      <Panel id="opportunities" title="Opportunities" icon={Compass} right={<Link className="quiet-link" to="/opportunities" data-testid="cc-opps-board">Full board</Link>}>
        <div className="cc-counts">
          {['DISCOVERED', 'QUALIFIED', 'AWAITING_AUTHORIZATION', 'STALE_REQUIRES_RECHECK', 'REJECTED', 'COMPLETED'].map(k =>
            <Count key={k} label={k} value={opps.counts[k] ?? 0} tone={oppTone[k]} />)}
        </div>
        <div className="table-container" style={{ maxHeight: 320, overflowY: 'auto' }}>
          <table className="data-table"><thead><tr><th>Repo # Issue</th><th>Status</th><th>Reward</th><th>Checked</th><th></th></tr></thead>
            <tbody>{opps.items.length ? opps.items.slice(0, 40).map(o => <tr key={o.id} data-testid={`cc-opp-row-${o.id}`}>
              <td><strong>{o.repository ? `${o.repository}#${o.issue_number ?? '?'}` : (o.source_identifier || o.source)}</strong>
                <small>{o.title || '—'}</small></td>
              <td><Status id={`cc-opp-status-${o.id}`} value={o.status || 'UNKNOWN'} />
                {o.qualification_status === 'QUALIFIED' && o.status === 'AWAITING_AUTHORIZATION' && <div className="cc-boundary">qualified · not authorized</div>}</td>
              <td>{isNum(o.reward) ? usd(o.reward) : <span className="nt">REWARD UNKNOWN</span>}</td>
              <td className="muted">{when(o.last_checked_at)}</td>
              <td>
                {o.status === 'DISCOVERED'
                  ? <Action id={`cc-qualify-${o.id}`} busy={busy === `qualify-${o.id}`} onClick={() => qualify(o.id)}>Qualify</Action>
                  : <Action id={`cc-recheck-${o.id}`} busy={busy === `recheck-${o.id}`} onClick={() => recheck(o.id)}>Recheck</Action>}
                {canRun(o.status) && <Action id={`cc-run-${o.id}`} primary icon={PlayCircle} busy={busy === `run-bounty-${o.id}`} onClick={() => setRunConfirm(o)}>Run bounty</Action>}
              </td>
            </tr>) : <tr><td colSpan={5} className="muted" data-testid="cc-opps-empty">No opportunities discovered yet.</td></tr>}</tbody>
          </table>
        </div>
        <p className="cc-boundary">Boundary: QUALIFIED → AWAITING_AUTHORIZATION → human approval. Qualification never authorizes execution.</p>
      </Panel>

      {/* FINANCE */}
      <Panel id="finance" title="Finance" icon={DollarSign}>
        <KV k="Run authorization" testid="cc-fin-auth"><Status id="cc-fin-auth-status" value={fin.run_budget.authorized ? 'AUTHORIZED' : 'NOT_AUTHORIZED'} /></KV>
        <KV k="Budget cap" testid="cc-fin-cap">{nano(fin.cap_nano_usd, 2)}</KV>
        <KV k="Reserved" testid="cc-fin-reserved">{nano(fin.reserved_nano_usd, 6)}</KV>
        <KV k="Provider-confirmed spent" testid="cc-fin-confirmed">{nano(fin.provider_confirmed_spent_nano_usd, 6)}</KV>
        <KV k="Remaining" testid="cc-fin-remaining">{nano(fin.remaining_nano_usd, 2)}</KV>
        <KV k="Escalation" testid="cc-fin-escalation">{fin.escalation.enabled ? 'ENABLED' : 'DISABLED'} · {fin.escalation.count} · {fin.escalation.spend_nano_usd === 'NOT_TRACKED' ? <span className="nt">SPEND NOT TRACKED</span> : nano(fin.escalation.spend_nano_usd, 6)}</KV>
        <KV k="Model requests" testid="cc-fin-requests">{fin.model_requests}</KV>
        <KV k="Revenue received" testid="cc-fin-revenue">{fin.revenue.verified_payments ? `${usd(fin.revenue.verified_revenue)} · ${fin.revenue.verified_payments} verified` : <span className="nt">NO REVENUE RECORDED</span>}</KV>
        <p className="cc-boundary">Reserved is a pre-charge against the cap, not confirmed spend. Provider-confirmed is the only recognized cost.</p>
      </Panel>

      {/* EXECUTION */}
      <Panel id="execution" title="Execution" icon={GitPullRequest} right={
        <div className="cc-controls" style={{ margin: 0 }}>
          <Action id="cc-exec-settings" busy={busy === 'open-policy'} onClick={openPolicy}>Execution settings</Action>
          <Link className="quiet-link" to="/jobs" data-testid="cc-exe-jobs">All jobs</Link>
        </div>}>
        <div className="cc-counts">
          <Count label="ACTIVE" value={exe.counts.ACTIVE} />
          <Count label="AWAITING_APPROVAL" value={exe.awaiting_approval} tone={exe.awaiting_approval ? 'warn' : ''} />
          <Count label="NEEDS_REVIEW" value={exe.counts.NEEDS_REVIEW} tone={exe.counts.NEEDS_REVIEW ? 'warn' : ''} />
          <Count label="BLOCKED" value={exe.counts.BLOCKED} tone={exe.counts.BLOCKED ? 'bad' : ''} />
          <Count label="FAILED" value={exe.counts.FAILED} tone={exe.counts.FAILED ? 'bad' : ''} />
          <Count label="COMPLETED" value={exe.counts.COMPLETED} tone="good" />
        </div>
        <KV k="Draft PRs staged (not published)" testid="cc-exe-staged">{exe.pull_requests.staged}</KV>
        <KV k="Published draft PRs" testid="cc-exe-published">{exe.pull_requests.published}</KV>
        {exe.jobs.length ? <div className="table-container" style={{ marginTop: 12, maxHeight: 240, overflowY: 'auto' }}>
          <table className="data-table"><thead><tr><th>Job</th><th>Status</th><th>Publication</th></tr></thead>
            <tbody>{exe.jobs.slice(0, 15).map(j => <tr key={j.id} data-testid={`cc-job-row-${j.id}`}>
              <td><strong>{j.title || j.id}</strong><small>{j.repository}{j.simulated ? ' · SAFE TEST' : ''}</small></td>
              <td><Status id={`cc-job-status-${j.id}`} value={j.status || 'UNKNOWN'} /></td>
              <td><Status id={`cc-job-pub-${j.id}`} value={j.pr_url ? (j.status === 'SUBMITTED' ? 'PUBLISHED' : 'DRAFT') : 'NOT_PUBLISHED'} /></td>
            </tr>)}</tbody></table>
        </div> : <p className="cc-boundary" data-testid="cc-exe-empty">No jobs yet. Execution requires explicit human authorization on the dedicated flow.</p>}
      </Panel>

      {/* SAFETY */}
      <Panel id="safety" title="Safety" icon={ShieldAlert}>
        <Metric k="Prompt-injection rejects" value={saf.prompt_injection_rejects} testid="cc-saf-injection" />
        <Metric k="Unsafe content rejects" value={saf.unsafe_content_rejects} testid="cc-saf-unsafe-content" />
        <Metric k="Unsafe-patch rejects" value={saf.unsafe_patch_rejects} testid="cc-saf-unsafe-patch" />
        <Metric k="Secret violations" value={saf.secret_violations} testid="cc-saf-secret" />
        <Metric k="Publication attempts" value={saf.publication_attempts} testid="cc-saf-pub-attempts" />
        <Metric k="Blocked publications" value={saf.blocked_publications} testid="cc-saf-pub-blocked" />
        <Metric k="Successful publications" value={saf.successful_publications} testid="cc-saf-pub-ok" />
        <Metric k="Authorization denials" value={saf.authorization_denials} testid="cc-saf-authz" />
        <Metric k="Blocked audit events" value={saf.blocked_events} testid="cc-saf-blocked-events" />
        <p className="cc-boundary">0 means the backend confirmed zero incidents. NOT TRACKED means no authoritative event source exists yet.</p>
      </Panel>

      {/* AUTONOMOUS */}
      <Panel id="autonomous" title="Autonomous" icon={Bot} right={<Action id="cc-open-controls" busy={busy === 'open-controls'} onClick={openControls}>Execution controls</Action>}>
        {ctrls ? <>
          <KV k="Autonomous execution" testid="cc-auto-exec"><Status id="cc-auto-exec-status" value={ctrls.autonomous_execution ? 'ENABLED' : 'DISABLED'} /></KV>
          <KV k="Autonomous escalation" testid="cc-auto-esc"><Status id="cc-auto-esc-status" value={ctrls.autonomous_escalation ? 'ENABLED' : 'DISABLED'} /></KV>
          <KV k="Background driver" testid="cc-driver-status"><Status id="cc-driver-status-val" value={ctrls.driver?.status || 'STOPPED'} /></KV>
          <KV k="Why" testid="cc-driver-reason">{ctrls.driver?.reason || '—'}</KV>
          <KV k="Eligible bounties" testid="cc-driver-eligible">{ctrls.driver?.eligible_count ?? 0}</KV>
          <KV k="Active job" testid="cc-driver-active">{ctrls.driver?.active_job || '—'}</KV>
          <KV k="Daily budget" testid="cc-budget-daily">{usd(ctrls.daily_budget_nano_usd / 1e9)}</KV>
          <KV k="Spent today" testid="cc-budget-spent">{usd(ctrls.spent_today_nano_usd / 1e9)}</KV>
          <KV k="Reserved today" testid="cc-budget-reserved">{usd(ctrls.reserved_today_nano_usd / 1e9)}</KV>
          <KV k="Remaining today" testid="cc-budget-remaining">{usd(ctrls.remaining_today_nano_usd / 1e9)}</KV>
          <KV k="Per-job cap" testid="cc-budget-perjob">{usd(ctrls.per_job_cap_nano_usd / 1e9)}</KV>
          <KV k="Max concurrent jobs" testid="cc-budget-concurrent">{ctrls.max_concurrent_jobs}</KV>
          <KV k="Last autonomous execution" testid="cc-driver-last">{when(ctrls.driver?.last_execution_at)}</KV>
          {ctrls.driver?.last_error && <p className="cc-boundary" data-testid="cc-driver-error" style={{ color: 'var(--red)' }}>Last stop/error: {ctrls.driver.last_error}</p>}
        </> : <p className="cc-boundary" data-testid="cc-controls-loading">Loading execution controls…</p>}
        <p className="cc-boundary">Backend stays authoritative. Autonomous mode still enforces reward VERIFIED, safety CLEARED, repo authorization, claim idempotency and review/publication gates, runs at most one job, and never auto-merges.</p>
      </Panel>
    </div>

    <Modal open={cfgModal} onClose={() => setCfgModal(false)} title="Discovery configuration" detail="Owner-only. Editing the query or page size does not authorize any bounty job or model spend." id="cc-config-modal">
      <Field label="Search query" id="cc-cfg-query" value={cfg.query} onChange={e => setCfg({ ...cfg, query: e.target.value })} placeholder="is:issue is:open label:bounty" />
      <div className="form-grid" style={{ marginTop: 16 }}>
        <Field label="Results per run (1–50)" id="cc-cfg-per-page" type="number" min={1} max={50} value={cfg.per_page} onChange={e => setCfg({ ...cfg, per_page: e.target.value })} />
        <label className="check-field" htmlFor="cc-cfg-enabled"><input id="cc-cfg-enabled" data-testid="cc-cfg-enabled" type="checkbox" checked={!!cfg.enabled} onChange={e => setCfg({ ...cfg, enabled: e.target.checked })} />Include in scheduled (background) discovery</label>
      </div>
      <div className="modal-actions">
        <Action id="cc-cfg-cancel" onClick={() => setCfgModal(false)}>Cancel</Action>
        <Action id="cc-cfg-save" primary busy={busy === 'save-config'} onClick={saveConfig}>Save configuration</Action>
      </div>
    </Modal>

    <Modal open={briefModal} onClose={() => setBriefModal(false)} title="Latest briefing" detail="Deterministic database aggregation. No model inference was used." id="cc-brief-modal">
      {brief && (brief.generated_at ? <div data-testid="cc-brief-body">
        <KV k="Generated" testid="cc-brief-generated">{when(brief.generated_at)}</KV>
        <KV k="Model used" testid="cc-brief-model">{brief.model_used || 'none (deterministic)'}</KV>
        <KV k="Provider-confirmed spend" testid="cc-brief-spend">{nano(brief.provider_confirmed_spend_nano_usd, 6)}</KV>
        <KV k="Pending approvals" testid="cc-brief-approvals">{brief.pending_approvals ?? 0}</KV>
        <KV k="Staged PRs" testid="cc-brief-staged">{brief.staged_prs ?? 0}</KV>
        <KV k="Published PRs" testid="cc-brief-published">{brief.published_prs ?? 0}</KV>
        <KV k="Opportunities" testid="cc-brief-opps">{Object.entries(brief.opportunities || {}).map(([k, v]) => `${k}:${v}`).join('  ') || '—'}</KV>
        <KV k="GitHub" testid="cc-brief-github">{brief.github_status} · write {brief.github_write}</KV>
      </div> : <p className="muted" data-testid="cc-brief-empty">No briefing generated yet. Use “Generate briefing”.</p>)}
      <div className="modal-actions"><Action id="cc-brief-close" onClick={() => setBriefModal(false)}>Close</Action></div>
    </Modal>

    <Modal open={!!runConfirm} onClose={() => setRunConfirm(null)} title="Run bounty" detail="Authorizes ONE real bounded execution: sandboxed clone, model attempts within the hard budget cap, and a DRAFT PR only. No merge is ever performed. For non-escrow (OWNER_APPROVAL) bounties this runs the coding work only — payout stays a human claim and the reward is never treated as guaranteed." id="cc-run-modal">
      {runConfirm && <>
        <KV k="Opportunity" testid="cc-run-opp">{runConfirm.repository ? `${runConfirm.repository}#${runConfirm.issue_number ?? '?'}` : runConfirm.id}</KV>
        <KV k="Status" testid="cc-run-status"><Status id="cc-run-status-val" value={runConfirm.status || 'UNKNOWN'} /></KV>
        <KV k="Reward" testid="cc-run-reward">{isNum(runConfirm.reward) && runConfirm.reward ? usd(runConfirm.reward) : <span className="nt">REWARD UNKNOWN</span>}</KV>
        <p className="cc-boundary">The backend re-verifies reward VERIFIED, QUALIFIED and safety CLEARED and refuses the run if any gate fails.</p>
      </>}
      <div className="modal-actions">
        <Action id="cc-run-cancel" onClick={() => setRunConfirm(null)}>Cancel</Action>
        <Action id="cc-run-confirm" primary busy={runConfirm && busy === `run-bounty-${runConfirm.id}`} onClick={doRun}>Confirm &amp; run</Action>
      </div>
    </Modal>

    <Modal open={stopConfirm} onClose={() => setStopConfirm(false)} title="Stop job" detail="Stopping cancels remaining model attempts, prevents escalation/corrections, blocks publication, terminates the active E2B sandbox, reconciles the budget and preserves the audit trail." id="cc-stop-modal">
      <div className="modal-actions">
        <Action id="cc-stop-cancel" onClick={() => setStopConfirm(false)}>Keep running</Action>
        <Action id="cc-stop-confirm" danger busy={stopping} onClick={doStop}>Stop job now</Action>
      </div>
    </Modal>

    <Modal open={polModal} onClose={() => setPolModal(false)} title="Execution policy" detail="Bounded attempts and corrections for manual bounty runs. Editing these never authorizes a run and never changes the budget cap." id="cc-exec-policy-modal">
      {pol && <>
        <div className="form-grid">
          <Field label="Max total model attempts (1–8)" id="cc-pol-total-attempts" type="number" min={1} max={8} value={pol.max_total_model_attempts} onChange={e => setPol({ ...pol, max_total_model_attempts: e.target.value })} />
          <Field label="Max attempts per model (1–4)" id="cc-pol-per-model" type="number" min={1} max={4} value={pol.max_attempts_per_model} onChange={e => setPol({ ...pol, max_attempts_per_model: e.target.value })} />
          <Field label="Max corrections per model (0–3)" id="cc-pol-corr-model" type="number" min={0} max={3} value={pol.max_corrections_per_model} onChange={e => setPol({ ...pol, max_corrections_per_model: e.target.value })} />
          <Field label="Max total corrections (0–6)" id="cc-pol-corr-total" type="number" min={0} max={6} value={pol.max_total_corrections} onChange={e => setPol({ ...pol, max_total_corrections: e.target.value })} />
          <Field label="Max timeout retries per model (0–3)" id="cc-pol-timeout-retries" type="number" min={0} max={3} value={pol.max_timeout_retries_per_model} onChange={e => setPol({ ...pol, max_timeout_retries_per_model: e.target.value })} />
          <Field label="Max model escalations (0–4)" id="cc-pol-escalations" type="number" min={0} max={4} value={pol.max_model_escalations} onChange={e => setPol({ ...pol, max_model_escalations: e.target.value })} />
        </div>
        <div style={{ marginTop: 14 }}>
          <KV k="Per-job budget cap (read-only)" testid="cc-pol-budget">{nano(pol.per_job_budget_nano_usd, 2)}</KV>
          <KV k="Timeouts (read-only)" testid="cc-pol-timeouts">routine {pol.timeouts?.routine}s · coding {pol.timeouts?.coding}s · flagship {pol.timeouts?.flagship_coding}s</KV>
        </div>
      </>}
      <div className="modal-actions">
        <Action id="cc-pol-cancel" onClick={() => setPolModal(false)}>Cancel</Action>
        <Action id="cc-pol-save" primary busy={busy === 'save-policy-exec'} onClick={savePolicy}>Save policy</Action>
      </div>
    </Modal>

    <Modal open={ctrlModal} onClose={() => setCtrlModal(false)} title="Execution controls" detail="Owner-only. Enabling autonomous execution causes REAL provider spend and REAL GitHub draft-PR writes. Backend gates are never bypassed; the driver runs at most one job at a time and never merges." id="cc-controls-modal">
      {cform && <>
        <label className="check-field" htmlFor="cc-cform-exec"><input id="cc-cform-exec" data-testid="cc-cform-exec" type="checkbox" checked={!!cform.autonomous_execution} onChange={e => setCform({ ...cform, autonomous_execution: e.target.checked })} />Autonomous execution</label>
        <label className="check-field" htmlFor="cc-cform-esc"><input id="cc-cform-esc" data-testid="cc-cform-esc" type="checkbox" checked={!!cform.autonomous_escalation} onChange={e => setCform({ ...cform, autonomous_escalation: e.target.checked })} />Autonomous model escalation</label>
        <div className="form-grid" style={{ marginTop: 16 }}>
          <Field label="Daily budget (USD)" id="cc-cform-daily" type="number" min={0} step="0.01" value={cform.daily_usd} onChange={e => setCform({ ...cform, daily_usd: e.target.value })} />
          <Field label="Per-job hard cap (USD)" id="cc-cform-perjob" type="number" min={0} step="0.01" value={cform.per_job_usd} onChange={e => setCform({ ...cform, per_job_usd: e.target.value })} />
        </div>
        {cform.autonomous_execution && !ctrls?.autonomous_execution && <label className="check-field" htmlFor="cc-cform-confirm" style={{ marginTop: 12 }}><input id="cc-cform-confirm" data-testid="cc-cform-confirm" type="checkbox" checked={!!cform.confirm} onChange={e => setCform({ ...cform, confirm: e.target.checked })} />I understand this spends real funds and writes real draft PRs to third-party repositories.</label>}
      </>}
      <div className="modal-actions">
        <Action id="cc-cform-cancel" onClick={() => setCtrlModal(false)}>Cancel</Action>
        <Action id="cc-cform-save" primary busy={busy === 'save-controls'} onClick={saveControls}>Save controls</Action>
      </div>
    </Modal>
  </>;
}
