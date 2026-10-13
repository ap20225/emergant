import React, { useState } from 'react';
import { toast } from 'sonner';
import { Action, Modal } from './UI';
import { api, attempt } from './api';

export const LiveJobActions = ({ job, reload }) => {
  const [confirm, setConfirm] = useState(false), [busy, setBusy] = useState(false);
  if (job.simulated) return null;
  const publish = async () => { setBusy(true); await attempt(async () => { await api(`/execution/${job.id}/publish`, 'POST', { confirm_publication: true }); setConfirm(false); await reload(); toast.success('GitHub confirmed a real draft PR. No merge performed.'); }); setBusy(false); };
  return <section className="connection-section" data-testid="live-job-delivery">
    <p data-testid="live-job-boundary">REAL REPOSITORY · {job.controlled_test ? 'CONTROLLED TEST · NO BOUNTY REWARD' : 'PAYMENT NOT ASSUMED'}</p>
    {job.status === 'APPROVED' && <Action primary id="publish-real-draft" onClick={() => setConfirm(true)}>Publish approved draft PR</Action>}
    {job.pr_url && <><a href={job.pr_url} target="_blank" rel="noreferrer" data-testid="real-pr-link">Open GitHub PR</a><Action id="sync-real-pr" onClick={() => attempt(async () => { await api(`/execution/${job.id}/sync`, 'POST'); await reload(); })}>Refresh PR status</Action></>}
    <Modal id="real-publication-dialog" open={confirm} onClose={() => setConfirm(false)} title="Publish this draft PR to GitHub?" detail="This creates a remote branch, commit and draft PR for the approved patch. No merge. An ambiguous result will not be retried."><p data-testid="publication-repository">{job.repository} · {job.branch}</p><div className="modal-actions"><Action id="cancel-real-publication" onClick={() => setConfirm(false)}>Cancel</Action><Action id="confirm-real-publication" primary busy={busy} onClick={publish}>Confirm draft publication</Action></div></Modal>
  </section>;
};