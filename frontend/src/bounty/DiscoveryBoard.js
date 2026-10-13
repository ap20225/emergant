import React, { useState, useEffect, useCallback } from 'react';
import { RefreshCw, Rocket, CheckCircle2, RotateCw, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';
import { api } from './api';
import { useBounty } from './Context';
import { Action, SectionHeading, Status } from './UI';

const LANES = ['ALL', 'RUNNABLE', 'AUTONOMOUS', 'OWNER_APPROVAL', 'WATCHLIST'];

const RunnableBadge = ({ r }) => {
  if (!r) return <span>—</span>;
  const map = { RUNNABLE: 'RUNNABLE', NEEDS_QUALIFICATION: 'NEEDS QUALIFY', NEEDS_RECHECK: 'NEEDS RECHECK', NOT_RUNNABLE: 'NOT RUNNABLE' };
  return <Status id={`disc-runnable-${r.state}`} value={map[r.state] || r.state} />;
};

export const DiscoveryBoard = () => {
  const { data: ctx } = useBounty();
  const authenticated = !!ctx?.user;
  const [board, setBoard] = useState(null);
  const [lane, setLane] = useState('ALL');
  const [busy, setBusy] = useState(false);
  const [rowBusy, setRowBusy] = useState(null);

  const load = useCallback(async () => {
    if (!authenticated) return;
    try { setBoard(await api('/discovery/board')); } catch (e) { /* non-fatal */ }
  }, [authenticated]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);

  const refresh = async () => {
    setBusy(true);
    try {
      const r = await api('/discovery/run', 'POST');
      toast.success(`Discovery: +${r.result.discovered} new, ${r.result.duplicates} duplicates, ${r.result.qualified} qualified`);
      await load();
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  const act = async (kind, item) => {
    setRowBusy(item.id + kind);
    try {
      if (kind === 'run') {
        const r = await api('/execution/bounty/run', 'POST', { opportunity_id: item.id, confirm: true, owner_approved: true, acknowledge_unverified_payout: true });
        toast.success(`Run started (${r.job_id}). Track it in the review queue.`);
      } else if (kind === 'qualify') {
        await api(`/opportunities/${item.id}/qualify`, 'POST');
        toast.success('Qualified — re-checking runnable state.');
      } else if (kind === 'recheck') {
        await api(`/opportunities/${item.id}/recheck`, 'POST');
        toast.success('Rechecked against live GitHub state.');
      }
      await load();
    } catch (e) { toast.error(e.message); } finally { setRowBusy(null); }
  };

  if (!authenticated) return <section className="exec-controls-section" data-testid="discovery-board"><SectionHeading title="Discovery board" detail="Multi-source bounty discovery across 6+ platforms" /><div className="auth-required-placeholder" data-testid="disc-auth-required"><ShieldCheck size={22}/><strong>Sign in to view bounty opportunities</strong><span>The discovery board shows live opportunities from GitHub, Opire, Algora, TaskBounty, Superteam and BountyHub. Sign in with the instance-owner account to access the full board.</span></div></section>;
  if (!board) return <section className="exec-controls-section" data-testid="discovery-board"><SectionHeading title="Discovery board" detail="Loading opportunity supply…" /></section>;
  const s = board.summary;
  const runnableList = board.runnable_now_list || [];
  const rows = board.items.filter(i => lane === 'ALL' ? true : lane === 'RUNNABLE' ? (i.runnable && i.runnable.state === 'RUNNABLE') : i.lane === lane);

  return <section className="exec-controls-section" data-testid="discovery-board">
    <SectionHeading title="Discovery board" detail="“Payable” ≠ runnable. Each bounty below shows if it can run now and, if not, exactly why.">
      <Action id="discovery-refresh" icon={RefreshCw} busy={busy} onClick={refresh}>Refresh discovery</Action>
    </SectionHeading>

    {runnableList.length > 0 && <div className="disc-runnable-strip" data-testid="disc-runnable-strip">
      <div className="disc-runnable-head"><CheckCircle2 size={15} /> <strong>Runnable right now</strong> · {s.runnable_now} pass every check</div>
      <div className="disc-runnable-chips">
        {runnableList.slice(0, 5).map(i => <div className="disc-runnable-chip" key={i.id} data-testid={`disc-runnable-chip-${i.id}`}>
          <span>{i.repository}#{i.issue_number}{i.reward_amount ? ` · ${i.reward_amount} ${i.reward_currency || ''}` : ''}</span>
          <Action id={`disc-run-strip-${i.id}`} primary icon={Rocket} busy={rowBusy === i.id + 'run'} onClick={() => act('run', i)}>Run</Action>
        </div>)}
      </div>
    </div>}

    <div className="disc-tiers" data-testid="disc-tiers">
      <div className="disc-tier disc-tier-exec"><span>REAL EXECUTABLE NOW</span><strong data-testid="disc-tier-exec">{(s.tiers && s.tiers.REAL_EXECUTABLE_NOW) || 0}</strong></div>
      <div className="disc-tier disc-tier-unverified"><span>PAYMENT UNVERIFIED</span><strong data-testid="disc-tier-unverified">{(s.tiers && s.tiers.PAYMENT_UNVERIFIED) || 0}</strong></div>
      <div className="disc-tier disc-tier-watch"><span>WATCHLIST / DISCOVERED</span><strong data-testid="disc-tier-watch">{(s.tiers && s.tiers.WATCHLIST_DISCOVERED) || 0}</strong></div>
    </div>
    <div className="disc-summary">
      <div><span>Total</span><strong data-testid="disc-total">{s.total}</strong></div>
      <div className="disc-summary-hot"><span>Runnable now</span><strong data-testid="disc-runnable-now">{s.runnable_now || 0}</strong></div>
      <div><span>Payable</span><strong data-testid="disc-payable">{s.payable_yes}</strong></div>
      <div><span>Owner approval</span><strong>{s.lanes.OWNER_APPROVAL || 0}</strong></div>
      <div><span>Watchlist</span><strong>{s.lanes.WATCHLIST || 0}</strong></div>
      <div><span>Last scout</span><strong data-testid="disc-last-scout">{board.last_scout_at ? new Date(board.last_scout_at).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }) : '—'}</strong></div>
    </div>
    <div className="disc-health">{board.source_health.map(h => <span key={h.id} data-testid={`disc-src-${h.id}`}>{h.id} <Status id={`disc-srcstatus-${h.id}`} value={h.status || 'UNKNOWN'} /> · {h.total_discovered || 0}</span>)}</div>
    <div className="disc-tabs">{LANES.map(l => <button key={l} data-testid={`disc-tab-${l}`} className={l === lane ? 'on' : ''} onClick={() => setLane(l)}>{l.replace('_', ' ')}{l === 'RUNNABLE' ? ` (${s.runnable_now || 0})` : l !== 'ALL' ? ` (${s.lanes[l] || 0})` : ''}</button>)}</div>
    <div className="disc-table" data-testid="disc-list">
      <div className="disc-row disc-head"><span>Source</span><span>Repo # issue</span><span>Reward</span><span>Priority</span><span>Can it run?</span><span>Why / next step</span><span>Action</span></div>
      {rows.slice(0, 200).map(i => { const r = i.runnable || {}; return <div className="disc-row disc-row-v2" key={i.id} data-testid={`disc-item-${i.id}`}>
        <span>{i.source}</span>
        <span>{i.source_url ? <a href={i.source_url} target="_blank" rel="noreferrer" className="quiet-link">{i.repository}#{i.issue_number}</a> : (i.repository ? `${i.repository}#${i.issue_number}` : '—')}</span>
        <span>{i.reward_amount ? `${i.reward_amount} ${i.reward_currency || ''}` : '—'}</span>
        <span><Status id={`disc-prio-${i.id}`} value={i.priority} /></span>
        <span><RunnableBadge r={r} /></span>
        <span className="disc-reason" data-testid={`disc-reason-${i.id}`}>{r.reason || (i.blocking_reasons || []).join(', ') || '—'}</span>
        <span className="disc-action-cell">
          {r.state === 'RUNNABLE' && <Action id={`disc-run-${i.id}`} primary icon={Rocket} busy={rowBusy === i.id + 'run'} onClick={() => act('run', i)}>Run</Action>}
          {r.state === 'NEEDS_QUALIFICATION' && r.remedy === 'qualify' && <Action id={`disc-qualify-${i.id}`} icon={CheckCircle2} busy={rowBusy === i.id + 'qualify'} onClick={() => act('qualify', i)}>Qualify</Action>}
          {r.state === 'NEEDS_RECHECK' && <Action id={`disc-recheck-${i.id}`} icon={RotateCw} busy={rowBusy === i.id + 'recheck'} onClick={() => act('recheck', i)}>Recheck</Action>}
          {r.state === 'NOT_RUNNABLE' && <button className="disc-run-disabled" data-testid={`disc-run-disabled-${i.id}`} disabled title={r.reason}>Run</button>}
        </span>
      </div>; })}
      {!rows.length && <p className="exec-controls-loading">No opportunities in this view.</p>}
    </div>
  </section>;
};
