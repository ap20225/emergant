import React, { useState, useEffect, useCallback } from 'react';
import { ClipboardCheck, Bot, Ban, ExternalLink, X } from 'lucide-react';
import { toast } from 'sonner';
import { Link } from 'react-router-dom';
import { api } from './api';
import { useBounty } from './Context';
import { SectionHeading, Action } from './UI';

const BUCKETS = [
  { key: 'NEEDS_YOUR_REVIEW', title: 'Needs your review', icon: ClipboardCheck, cls: 'rq-you', bulk: false,
    empty: 'Nothing is waiting on your decision.' },
  { key: 'DECLINED_BY_ENGINE', title: 'Declined by the engine', icon: Bot, cls: 'rq-declined', bulk: true,
    empty: 'No engine-declined runs.' },
  { key: 'BLOCKED_EXTERNALLY', title: 'Blocked externally', icon: Ban, cls: 'rq-blocked', bulk: true,
    empty: 'Nothing blocked by upstream repos.' },
];

export const ReviewQueue = () => {
  const { data: ctx } = useBounty();
  const authenticated = !!ctx?.user;
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(null);

  const load = useCallback(async () => {
    if (!authenticated) return;
    try { setData(await api('/execution/review-queue')); } catch (e) { /* non-fatal */ }
  }, [authenticated]);
  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t); }, [load]);

  const dismiss = async (id) => {
    setBusy(id);
    try { await api(`/jobs/${id}/archive`, 'POST'); await load(); }
    catch (e) { toast.error(e.message); } finally { setBusy(null); }
  };
  const dismissBucket = async (bucket) => {
    setBusy(bucket);
    try {
      const r = await api('/jobs/archive-bulk', 'POST', { bucket });
      toast.success(`Dismissed ${r.archived_count} item${r.archived_count === 1 ? '' : 's'}.`);
      await load();
    } catch (e) { toast.error(e.message); } finally { setBusy(null); }
  };

  if (!data) return null;
  const counts = data.counts || {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);

  return <section className="exec-controls-section review-queue-section" data-testid="review-queue">
    <SectionHeading title="Review queue" detail="Sorted into honest buckets — “Needs your review” means it needs YOU. Dismiss the rest." />
    <div className="rq-buckets">
      {BUCKETS.map(b => {
        const items = (data.buckets && data.buckets[b.key]) || [];
        const Icon = b.icon;
        return <div className={`rq-bucket ${b.cls}`} key={b.key} data-testid={`rq-bucket-${b.key}`}>
          <div className="rq-bucket-head">
            <h3><Icon size={14} /> {b.title}</h3>
            <span className="rq-count" data-testid={`rq-count-${b.key}`}>{items.length}</span>
          </div>
          {items.length === 0 ? <p className="rq-empty">{b.empty}</p>
            : items.slice(0, 8).map(it => <div className="rq-item" key={it.id} data-testid={`rq-item-${it.id}`}>
                <div className="rq-item-top">
                  {it.repository ? <Link to={`/jobs/${it.id}`}>{it.repository}#{it.issue_number}</Link> : <strong>{it.title || it.id}</strong>}
                  {it.pr_url ? <a href={it.pr_url} target="_blank" rel="noreferrer" title="Draft PR"><ExternalLink size={12} /></a> : null}
                </div>
                <p>{it.outcome}</p>
                <div className="rq-item-actions">
                  {b.key === 'NEEDS_YOUR_REVIEW'
                    ? <Link className="rq-dismiss" to={`/jobs/${it.id}`} data-testid={`rq-open-${it.id}`}>Open</Link>
                    : <button className="rq-dismiss" data-testid={`rq-dismiss-${it.id}`} disabled={busy === it.id} onClick={() => dismiss(it.id)}><X size={11} /> Dismiss</button>}
                </div>
              </div>)}
          {b.bulk && items.length > 1 && <button className="rq-bucket-bulk" data-testid={`rq-bulk-${b.key}`} disabled={busy === b.key} onClick={() => dismissBucket(b.key)}>Dismiss all {items.length}</button>}
        </div>;
      })}
    </div>
    {total === 0 && <p className="rq-empty" data-testid="rq-all-clear">Your review queue is clear. 🎯</p>}
  </section>;
};
