import React, { useCallback, useEffect, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { Activity, ArrowDownToLine, ArrowUpRight, Bot, Brain, ChevronDown, CircleStop, Code2, Compass, FileClock, FolderGit2, GitPullRequest, LayoutDashboard, LogOut, Menu, Play, Radar, Receipt, Settings2, ShieldCheck, SquareTerminal, Wallet, X } from 'lucide-react';
import { toast } from 'sonner';
import { BountyContext } from './Context';
import { api, attempt, money, onSessionExpired } from './api';
import { Action, Modal, Status } from './UI';

const navigation = [
  ['/', 'Dashboard', LayoutDashboard], ['/command-center', 'Command Center', Radar], ['/opportunities', 'Opportunities', Compass], ['/jobs', 'Active Jobs', SquareTerminal],
  ['/repositories', 'Repositories', FolderGit2], ['/agents', 'Agents', Bot], ['/pull-requests', 'Pull Requests', GitPullRequest],
  ['/payments', 'Payments', Receipt], ['/finance', 'Finance', Wallet], ['/learning', 'Learning', Brain],
  ['/security', 'Security', ShieldCheck], ['/audit-log', 'Audit Log', FileClock], ['/settings', 'Settings', Settings2],
];

export default function CommandLayout() {
  const [data, setData] = useState(null), [error, setError] = useState(''), [menu, setMenu] = useState(false);
  const [stopModal, setStopModal] = useState(false), [busy, setBusy] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);
  const navigate = useNavigate(), location = useLocation();
  const refresh = useCallback(async () => { try { setData(await api('/bootstrap')); setError(''); } catch (e) { setError(e.message); } }, []);
  useEffect(() => { refresh(); const interval = setInterval(refresh, 4000); return () => clearInterval(interval); }, [refresh]);
  useEffect(() => { onSessionExpired(() => setSessionExpired(true)); }, []);
  useEffect(() => { setMenu(false); }, [location.pathname]);
  const discover = async () => {
    setBusy(true);
    await attempt(async () => { const opportunity = await api('/safe-test/discover', 'POST'); await refresh(); navigate('/opportunities', { state: { selected: opportunity.id } }); toast.success('Local acceptance fixture discovered and scored. No real bounty or reward.'); });
    setBusy(false);
  };
  const control = async (action) => attempt(async () => { setStopModal(false); const controls = await api('/controls', 'POST', { action }); setData(prev => ({ ...prev, controls })); await refresh(); toast.success(action === 'RESUME' ? 'Operations resumed. Budget limits unchanged.' : 'Emergency control applied.'); });
  if (!data) return <div className="initial-loading" data-testid="app-loading"><Code2 size={32}/><h2 data-testid="loading-brand-name">Bounty Hunter</h2><p>{error || 'Connecting to command center…'}</p>{error && <Action id="initial-retry" onClick={refresh}>Retry connection</Action>}</div>;
  const navName = navigation.find(([path]) => path === location.pathname)?.[1] || 'Job workspace';
  return <BountyContext.Provider value={{ data, refresh, discover, busy, control }}><div className="command-app">
    {menu && <button className="sidebar-scrim" data-testid="sidebar-scrim" aria-label="Close navigation" onClick={() => setMenu(false)}/>}
    <aside className={`sidebar ${menu ? 'open' : ''}`}>
      <NavLink to="/" className="brand" data-testid="brand-home"><span className="brand-mark"><Code2 size={23}/></span><span className="brand-name"><span data-testid="app-brand-name">Bounty Hunter</span><span className="brand-caption">BOUNTY OPERATING SYSTEM</span></span></NavLink>
      <div className="workspace-switch"><span className="workspace-avatar">P</span><span>Personal workspace<small>Bootstrap program</small></span><ChevronDown size={14}/></div>
      <div className="nav-group-label">WORKSPACE</div>
      <nav>{navigation.map(([path, name, Icon], i) => <React.Fragment key={path}>{i === 10 && <div className="nav-divider"/>}<NavLink end={path === '/'} to={path} data-testid={`nav-${path === '/' ? 'dashboard' : path.slice(1)}`} className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}><Icon size={17}/><span>{name}</span>{name === 'Pull Requests' && data.metrics.awaiting_approval > 0 && <em data-testid="nav-approval-count">{data.metrics.awaiting_approval}</em>}</NavLink></React.Fragment>)}</nav>
      <div className="sidebar-bottom"><div className="bootstrap-label"><span className="level-icon">0{data.finance.level}</span><div>Bootstrap level {data.finance.level}<small>From zero. Built on proof.</small></div></div><div className="bootstrap-track"><span style={{ width: `${Math.min(100, data.finance.verified_revenue / 25 * 100)}%` }}/></div><div className="bootstrap-numbers"><span>{money(data.finance.verified_revenue)} verified</span><span>$25.00</span></div><div className="sidebar-footer"><span><i className="dot green"/> Database connected</span><span>v0.1</span></div></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-menu" data-testid="mobile-menu-toggle" aria-label="Open navigation" onClick={() => setMenu(true)}><Menu size={20}/></button><span className="breadcrumb-brand">Workspace</span><span className="slash">/</span><span>{navName}</span></div><div className="topbar-actions"><span className="mode-pill" data-testid="autonomy-mode"><ShieldCheck size={13}/>{data.settings.mode}</span><button className="emergency-button" data-testid="emergency-global-stop-button" onClick={() => setStopModal(true)}><CircleStop size={14}/><span>Global stop</span></button>{data.user ? <button className="profile-avatar" data-testid="account-menu-button" title="Sign out" onClick={() => attempt(async () => { await fetch(`${process.env.REACT_APP_BACKEND_URL}/api/auth/logout`, { method: 'POST', credentials: 'include' }); await refresh(); })}>{data.user.name?.slice(0, 2).toUpperCase()}</button> : <NavLink to="/login" data-testid="owner-sign-in" className="sign-in-link">Sign in <ArrowUpRight size={13}/></NavLink>}</div></header>
      {error && <div className="system-banner error" data-testid="connection-error"><Activity size={15}/>{error}<button data-testid="refresh-connection" onClick={refresh}>Retry</button></div>}
      {data.controls.global_stop && <div className="system-banner error" data-testid="global-stop-alert"><CircleStop size={15}/> Global stop active. Agents and external actions are paused.<button data-testid="resume-operations-button" onClick={() => control('RESUME')}>Resume operations</button></div>}
      {(data.preview || sessionExpired) && <div className="auth-gate-banner" data-testid="auth-gate-banner"><div className="auth-gate-inner"><ShieldCheck size={20}/><div><strong>Session required</strong><span>Sign in with the instance-owner Google account to view bounties, execution controls and all operations data.</span></div><NavLink to="/login" data-testid="auth-gate-sign-in" className="action primary link-action" style={{ padding: '8px 20px', fontSize: 14 }}>Sign in <ArrowUpRight size={13}/></NavLink></div></div>}
      {data.preview && <div className="preview-strip" data-testid="read-only-preview"><ShieldCheck size={13}/> Read-only workspace <span>Owner sign-in required for actions. Existing accounts are preserved.</span></div>}
      <main className="content" key={location.pathname}><Outlet/></main>
      <footer className="main-footer"><span><i className="dot green"/> All decisions leave a trail.</span><span>ZERO CAPITAL. VERIFIED OUTCOMES.</span></footer>
    </div>
    <Modal open={stopModal} onClose={() => setStopModal(false)} title="Stop all operations?" detail="Active reference processes will be stopped. Workspaces, reports and audit history are preserved. No automatic restart." id="global-stop-dialog"><div className="modal-warning"><CircleStop size={20}/>Stops agents, new jobs, spending and submissions.</div><div className="modal-actions"><Action id="global-stop-cancel" onClick={() => setStopModal(false)}>Cancel</Action><Action danger id="global-stop-confirm" icon={CircleStop} onClick={() => control('GLOBAL_STOP')}>Stop all operations</Action></div></Modal>
  </div></BountyContext.Provider>;
}