import React, { useState, useEffect } from 'react';
import { Compass, X } from 'lucide-react';
import { Action } from './UI';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';

const STEPS = [
  { title: 'Welcome to Bounty Hunter', body: 'This is your operations dashboard — the path from discovering a coding bounty to a verified payment, with every dollar of spend gated and approved.' },
  { title: 'Discover & qualify', body: 'Use “Discover opportunities” to find bounties. Each is checked for a VERIFIED reward, CLEARED safety and an authorized repository before it can ever run.' },
  { title: 'Autonomous execution', body: 'The “Autonomous execution” panel is your control room. Toggle the background driver on/off and set your budgets. Enabling it needs an explicit confirmation because it spends real money and opens real draft PRs.' },
  { title: 'Budgets stay in control', body: 'A master daily budget (default $25) caps total autonomous spend; a per-job hard cap (default $0.25) means no single job can drain it. Nothing ever auto-merges.' },
  { title: 'Watch it work', body: 'The driver runs at most one job at a time and always shows why it is idle (e.g. NO_ELIGIBLE_BOUNTIES). Track live jobs in the Command Center and Active Jobs.' },
];
const KEY = 'bh_walkthrough_done_v1';

export const Walkthrough = ({ forceOpen, onClose }) => {
  const [open, setOpen] = useState(false);
  const [i, setI] = useState(0);

  useEffect(() => { if (!localStorage.getItem(KEY)) setOpen(true); }, []);
  useEffect(() => { if (forceOpen) { setI(0); setOpen(true); } }, [forceOpen]);

  const close = () => { localStorage.setItem(KEY, '1'); setOpen(false); onClose && onClose(); };
  const step = STEPS[i];

  return <Dialog open={open} modal onOpenChange={v => !v && close()}>{open && (
    <DialogContent data-testid="walkthrough-modal" className="bounty-modal walkthrough-modal">
      <div className="walkthrough-badge"><Compass size={15} />Guided tour · {i + 1}/{STEPS.length}</div>
      <DialogTitle>{step.title}</DialogTitle>
      <DialogDescription>{step.body}</DialogDescription>
      <button className="modal-close" data-testid="walkthrough-close" aria-label="Close tour" onClick={close}><X size={18} /></button>
      <div className="walkthrough-dots">{STEPS.map((_, k) => <i key={k} className={k === i ? 'on' : ''} />)}</div>
      <div className="modal-actions">
        {i > 0 && <Action id="walkthrough-back" onClick={() => setI(i - 1)}>Back</Action>}
        {i < STEPS.length - 1
          ? <Action id="walkthrough-next" primary onClick={() => setI(i + 1)}>Next</Action>
          : <Action id="walkthrough-finish" primary onClick={close}>Get started</Action>}
      </div>
    </DialogContent>
  )}</Dialog>;
};
