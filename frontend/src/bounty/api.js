import { toast } from 'sonner';

const origin = process.env.REACT_APP_BACKEND_URL;
if (!origin) throw new Error('REACT_APP_BACKEND_URL is required');

let _onSessionExpired = null;
export const onSessionExpired = (handler) => { _onSessionExpired = handler; };

export const api = async (path, method = 'GET', body) => {
  const response = await fetch(`${origin}/api/bounty${path}`, {
    method, credentials: 'include', headers: { 'Content-Type': 'application/json' },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const data = await response.json();
  if (!response.ok) {
    const isSessionAuth = response.status === 401 && !String(data.detail).includes('GitHub');
    if (isSessionAuth && _onSessionExpired) _onSessionExpired();
    const message = isSessionAuth
      ? 'Sign in with the existing instance-owner account to continue.'
      : typeof data.detail === 'string' ? data.detail : (data.detail?.map?.(d => d.msg).join('; ') || 'Request could not be completed');
    throw new Error(message);
  }
  return data;
};
export const downloadJson = async (path, filename) => {
  const response = await fetch(`${origin}/api/bounty${path}`, {
    method: 'GET', credentials: 'include', headers: { Accept: 'application/json' },
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const message = response.status === 401 && !String(data.detail).includes('GitHub')
      ? 'Sign in with the existing instance-owner account to continue.'
      : typeof data.detail === 'string' ? data.detail : (data.detail?.map?.(d => d.msg).join('; ') || 'Request could not be completed');
    throw new Error(message);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
};
export const attempt = async (callback) => {
  try { return await callback(); } catch (error) { toast.error(error.message); return null; }
};
export const money = (value = 0) => value === null ? 'Pending' : new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(value);
export const when = (date) => date ? new Date(date).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
export const label = (value = '') => value.replaceAll('_', ' ');