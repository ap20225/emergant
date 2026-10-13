import React, { useEffect } from 'react';
import { ArrowUpRight, Inbox, Loader2, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';

export const Status = ({ value = 'UNKNOWN', id, className = '' }) => <span data-testid={id} className={`status status-${value.toLowerCase().replaceAll(/[^a-z0-9]/g, '-')} ${className}`}><i />{value.replaceAll('_', ' ')}</span>;
export const Action = ({ children, id, icon: Icon, primary, danger, busy, ...props }) => <Button data-testid={id} className={`action ${primary ? 'primary' : ''} ${danger ? 'danger' : ''}`} {...props} disabled={busy || props.disabled}>{busy ? <Loader2 size={15} className="spin" /> : Icon ? <Icon size={15} /> : null}{children}</Button>;
export const PageHeading = ({ eyebrow, title, description, children }) => <div className="page-heading"><div><div className="eyebrow" data-testid="page-eyebrow">{eyebrow || 'OPERATIONS / BOUNTY HUNTER'}</div><h1 data-testid="page-title">{title}</h1>{description && <p>{description}</p>}</div><div className="heading-actions">{children}</div></div>;
export const SectionHeading = ({ title, detail, children }) => <div className="section-heading"><div><h2>{title}</h2>{detail && <span>{detail}</span>}</div>{children}</div>;
export const Empty = ({ title, detail, icon: Icon = Inbox, children, id = 'empty-state' }) => <div className="empty-state" data-testid={id}><div className="empty-icon"><Icon size={27} strokeWidth={1.4} /></div><h3>{title}</h3><p>{detail}</p>{children}</div>;
export const Modal = ({ open, onClose, title, detail, id, children }) => {
  useEffect(() => {
    if (open) return;
    // Controlled closure must release the document even if a dismissable layer
    // retained an outdated pointer lock. Never unlock another active modal/menu.
    const release = () => {
      if (!document.querySelector('[role="dialog"][data-state="open"], [role="alertdialog"][data-state="open"], [role="menu"][data-state="open"]')) {
        document.body.style.removeProperty('pointer-events');
      }
    };
    release();
    const timer = window.setTimeout(release, 220);
    return () => window.clearTimeout(timer);
  }, [open]);
  return <Dialog open={open} modal={open} onOpenChange={v => !v && onClose()}>{open && <DialogContent data-testid={id} className="bounty-modal"><DialogTitle>{title}</DialogTitle><DialogDescription>{detail}</DialogDescription><button className="modal-close" data-testid={`${id}-close`} aria-label="Close dialog" onClick={onClose}><X size={18} /></button>{children}</DialogContent>}</Dialog>;
};
export const Field = ({ label, id, children, ...props }) => <label className="field" htmlFor={id}><span>{label}</span>{children || <input id={id} data-testid={id} {...props} />}</label>;
export const Metric = ({ title, value, detail, icon: Icon, accent, id }) => <div className={`metric ${accent ? 'metric-accent' : ''}`} data-testid={`${id}-panel`}><div className="metric-label">{title}{Icon && <Icon size={16} />}</div><strong data-testid={id}>{value}</strong><div className="metric-detail">{detail}</div></div>;
export const TextLink = ({ children, ...props }) => <a {...props}>{children}<ArrowUpRight size={13}/></a>;