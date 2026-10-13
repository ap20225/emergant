import React, { useState, useEffect, useCallback } from 'react';
import { GitFork, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';
import { api } from './api';
import { useBounty } from './Context';
import { Action, Modal, Field, Status, SectionHeading, Empty } from './UI';

export const RepoAuthorization = () => {
  const { data: ctx } = useBounty();
  const authenticated = !!ctx?.user;
  const [repos, setRepos] = useState(null);
  const [modal, setModal] = useState(false);
  const [form, setForm] = useState({ upstream: '', fork_repo: '' });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!authenticated) return;
    try { const r = await api('/repos'); setRepos(r.repositories || []); } catch (e) { /* non-fatal */ }
  }, [authenticated]);
  useEffect(() => { load(); }, [load]);

  const authorize = async () => {
    if (!form.upstream.includes('/') || !form.fork_repo.includes('/')) {
      toast.error('Enter both repos as owner/name (e.g. facebook/react and myuser/react).'); return;
    }
    setBusy(true);
    try {
      const res = await api('/repos/fork/authorize', 'POST', { upstream: form.upstream.trim(), fork_repo: form.fork_repo.trim() });
      toast.success(res.write_ready ? `Fork authorized — ${form.upstream} is now write-ready via ${form.fork_repo}.` : 'Saved, but the fork is not write-ready yet.');
      setModal(false); setForm({ upstream: '', fork_repo: '' }); load();
    } catch (e) { toast.error(e.message); } finally { setBusy(false); }
  };

  const authorized = (repos || []).filter(r => r.status === 'AUTHORIZED');

  return <section className="exec-controls-section" data-testid="cc-panel-repos">
    <SectionHeading title="Repository authorization" detail="Pre-authorize a fork you control so a qualifying third-party bounty can run without a manual write step">
      <Action id="cc-open-fork-auth" icon={GitFork} onClick={() => setModal(true)} disabled={!authenticated}>Pre-authorize fork</Action>
    </SectionHeading>
    {!authenticated
      ? <div className="auth-required-placeholder" data-testid="cc-repos-auth-required"><ShieldCheck size={22}/><strong>Sign in to manage repositories</strong><span>Repository authorization and fork pre-approval require the instance owner session.</span></div>
      : repos === null
      ? <p className="exec-controls-loading" data-testid="cc-repos-loading">Loading repositories…</p>
      : authorized.length
        ? <div className="disc-table" data-testid="cc-repos-table">
            <div className="disc-row disc-head" style={{ gridTemplateColumns: '1.4fr 1.4fr .8fr 1fr' }}><span>Upstream / fork</span><span>Fork target</span><span>Status</span><span>Write capability</span></div>
            {authorized.map(r => <div className="disc-row" key={r.id} style={{ gridTemplateColumns: '1.4fr 1.4fr .8fr 1fr' }} data-testid={`cc-repo-${r.id}`}>
              <span>{r.full_name}</span>
              <span>{r.fork_repo || (r.fork_strategy === 'DIRECT_WRITE' ? 'direct write' : '—')}</span>
              <span><Status id={`cc-repo-status-${r.id}`} value={r.status} /></span>
              <span>{(r.capabilities || {}).CREATE_PR ? 'CREATE_PR' : '—'}</span>
            </div>)}
          </div>
        : <Empty id="cc-repos-empty" icon={ShieldCheck} title="No authorized write targets" detail="Fork a bounty's repo into an account your connected GitHub token can push to, then pre-authorize it here. Reads stay open; writes are never auto-granted." />}

    <Modal open={modal} onClose={() => setModal(false)} title="Pre-authorize a fork target"
      detail="Owner-only. The fork must be a real GitHub fork of the upstream that your connected GitHub account can push to. Draft-PR-only; the upstream is never written to directly and nothing is ever merged."
      id="cc-fork-auth-modal">
      <div className="form-grid">
        <Field label="Upstream repository (owner/name)" id="cc-fa-upstream" placeholder="facebook/react" value={form.upstream} onChange={e => setForm({ ...form, upstream: e.target.value })} />
        <Field label="Your fork (owner/name)" id="cc-fa-fork" placeholder="myuser/react" value={form.fork_repo} onChange={e => setForm({ ...form, fork_repo: e.target.value })} />
      </div>
      <div className="modal-actions">
        <Action id="cc-fa-cancel" onClick={() => setModal(false)}>Cancel</Action>
        <Action id="cc-fa-save" primary busy={busy} onClick={authorize}>Authorize fork</Action>
      </div>
    </Modal>
  </section>;
};
