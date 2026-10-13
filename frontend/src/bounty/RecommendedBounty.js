import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Target, RefreshCw, Rocket, ExternalLink, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';
import { api } from './api';
import { useBounty } from './Context';
import { Action, Modal, Status, SectionHeading, Empty } from './UI';

const Row = ({ label, children, testid }) => <div className="rec-row">
  <span className="rec-row-label">{label}</span>
  <span className="rec-row-value" data-testid={testid}>{children}</span>
</div>;

export const RecommendedBounty = () => {
  const { data: ctx } = useBounty();
  const authenticated = !!ctx?.user;
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [job, setJob] = useState(null);
  const poll = useRef(null);

  const load = useCallback(async (refresh = false) => {
    if (!authenticated) { setLoading(false); return; }
    setLoading(true);
    try { setData(await api(`/execution/recommended-candidate${refresh ? '?refresh=true' : ''}`)); }
    catch (e) { toast.error(e.message); }
    finally { setLoading(false); }
  }, [authenticated]);
  useEffect(() => { load(); return () => poll.current && clearInterval(poll.current); }, [load]);

  const rec = data && data.recommended;

  const pollJob = (jobId) => {
    poll.current && clearInterval(poll.current);
    poll.current = setInterval(async () => {
      try {
        const s = await api(`/execution/bounty/${jobId}/state`);
        setJob(s);
        if (['SUBMITTED', 'NEEDS_REVIEW', 'STOPPED', 'FAILED', 'PUBLISHED', 'DRAFT_PR'].includes(s.stage)) {
          clearInterval(poll.current);
        }
      } catch (e) { /* keep polling */ }
    }, 6000);
  };

  const runBounty = async () => {
    setBusy(true);
    try {
      if (!rec.checks.write_ready && rec.suggested_fork_repo) {
        toast.info('Creating & authorizing your fork…');
        await api('/repos/fork/authorize', 'POST', { upstream: rec.repository, fork_repo: rec.suggested_fork_repo, create_fork: true });
      }
      const r = await api('/execution/bounty/run', 'POST', {
        opportunity_id: rec.opportunity_id, confirm: true, owner_approved: true, acknowledge_unverified_payout: true,
      });
      toast.success('Real bounty run started.');
      setConfirm(false);
      setJob({ stage: r.stage || 'QUEUED', job_id: r.job_id });
      pollJob(r.job_id);
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  return <section className="exec-controls-section" data-testid="cc-recommended-bounty">
    <SectionHeading title="Recommended bounty" detail="One bounded, payable, open, executable candidate — verified against authoritative reward data + live GitHub state. Nothing runs until you click RUN BOUNTY.">
      <Action id="cc-rec-refresh" icon={RefreshCw} busy={loading} onClick={() => load(true)} disabled={!authenticated}>Re-scan</Action>
    </SectionHeading>

    {!authenticated ? <div className="auth-required-placeholder" data-testid="cc-rec-auth-required"><ShieldCheck size={22}/><strong>Sign in to view recommended bounties</strong><span>The recommendation engine selects bounded, payable candidates from live sources. Sign in to access.</span></div>
      : loading && !data ? <p className="exec-controls-loading" data-testid="cc-rec-loading">Scanning live sources…</p>
      : !rec ? <Empty id="cc-rec-empty" icon={Target} title="No bounded candidate right now"
          detail={`Checked ${(data && data.live_checked) || 0} of ${(data && data.considered) || 0} qualified rewards live. None currently pass PAYABLE + OPEN + AUTHORIZED + BOUNDED + TESTABLE. Re-scan later — Continuous Radar keeps supply fresh.`} />
      : <div className="rec-card" data-testid="cc-rec-card">
          <div className="rec-head">
            <div>
              <span className="rec-source" data-testid="cc-rec-source">{rec.source}</span>
              <a className="rec-title" href={rec.source_url} target="_blank" rel="noreferrer" data-testid="cc-rec-title">
                {rec.repository}#{rec.issue_number} <ExternalLink size={13} />
              </a>
              <p className="rec-issue-title" data-testid="cc-rec-issue-title">{rec.title}</p>
            </div>
            <div className="rec-reward" data-testid="cc-rec-reward">{rec.reward_amount} {rec.reward_currency}</div>
          </div>
          <div className="rec-grid">
            <Row label="Authoritative reward" testid="cc-rec-verif">{rec.verification_source}</Row>
            <Row label="Live GitHub status" testid="cc-rec-gh">
              <Status id="cc-rec-gh-status" value={(rec.checks.github_state || 'unknown').toUpperCase()} /> {rec.checks.is_pull_request ? '(PR)' : ''}
            </Row>
            <Row label="Repository" testid="cc-rec-repo">{rec.checks.language || '—'} · {rec.checks.size_kb ? Math.round(rec.checks.size_kb / 1024) + ' MB' : '—'} · {rec.checks.archived ? 'archived' : 'active'}</Row>
            {rec.repo_quality && <Row label="Repo quality" testid="cc-rec-quality">
              <Status id="cc-rec-quality-status" value={rec.repo_quality.confidence === 'NONE' ? 'CLEAN' : rec.repo_quality.confidence + '_RISK'} />
              {rec.repo_quality.signals && rec.repo_quality.signals.length ? ' · ' + rec.repo_quality.signals.join(', ') : ' · no synthetic signals'}
            </Row>}
            {rec.checks.inspection_detail && <Row label="Relevant context" testid="cc-rec-context">{rec.checks.inspection_detail}</Row>}
            <Row label="Qualification" testid="cc-rec-qual"><Status id="cc-rec-qual-status" value={rec.qualification_status} /> · risk <Status id="cc-rec-risk-status" value={rec.risk_status} /></Row>
            <Row label="Authorization" testid="cc-rec-auth">{rec.authorization_status}</Row>
            <Row label="Boundedness" testid="cc-rec-bounded">
              <Status id="cc-rec-bounded-status" value={rec.checks.bounded ? 'BOUNDED' : 'UNBOUNDED'} /> · {rec.boundedness_evidence}
            </Row>
            <Row label="Competition" testid="cc-rec-comp">solvers: {rec.solver_count ?? '—'} · assigned: {String(!!rec.checks.assigned)} · comments: {rec.checks.comments ?? '—'}</Row>
            <Row label="Suitability" testid="cc-rec-score">score {rec.score} · testable {String(!!rec.checks.testable)} · payout stays human-claim (OWNER_APPROVAL)</Row>
          </div>
          <div className="rec-actions">
            <Action id="cc-rec-run" primary icon={Rocket} disabled={!!(job && job.job_id)} onClick={() => setConfirm(true)}>RUN BOUNTY</Action>
          </div>

          {job && <div className="rec-job" data-testid="cc-rec-job">
            <strong>Run {job.job_id}</strong> — stage: <Status id="cc-rec-job-stage" value={job.stage || 'QUEUED'} />
            {job.attempt ? ` · attempt ${job.attempt}/${job.max_attempts || '?'}` : ''}
            {job.pr && job.pr.url ? <> · <a href={job.pr.url} target="_blank" rel="noreferrer" data-testid="cc-rec-pr">Draft PR</a></> : ''}
            {job.next_action ? ` · ${job.next_action}` : ''}
            {job.error ? <div className="rec-job-err">{String(job.error).slice(0, 160)}</div> : null}
          </div>}
        </div>}

    <Modal open={confirm} onClose={() => setConfirm(false)} id="cc-rec-confirm"
      title="Run this real bounty?"
      detail="This runs ONE real bounty through the existing pipeline (E2B, bounded attempts, $0.25 hard cap, draft-PR-only, no merge). If needed it first creates & authorizes a fork you control. Payout stays a human claim — the reward is never treated as guaranteed and the Opire creator-approval step is preserved.">
      {rec && <div className="rec-confirm-body">
        <div><strong>{rec.repository}#{rec.issue_number}</strong> · {rec.reward_amount} {rec.reward_currency}</div>
        <div>Fork target: {rec.checks && rec.checks.write_ready ? 'already authorized' : (rec.suggested_fork_repo || 'auto')}</div>
      </div>}
      <div className="modal-actions">
        <Action id="cc-rec-confirm-cancel" onClick={() => setConfirm(false)}>Cancel</Action>
        <Action id="cc-rec-confirm-run" primary busy={busy} icon={Rocket} onClick={runBounty}>Confirm & run</Action>
      </div>
    </Modal>
  </section>;
};
