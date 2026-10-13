import React from "react";
import "@/App.css";
import { BrowserRouter, Routes, Route, useLocation, Navigate } from "react-router-dom";
import LoginPage from "@/pages/LoginPage";
import AuthCallback from "@/pages/AuthCallback";
import { Toaster } from "@/components/ui/sonner";
import CommandLayout from '@/bounty/Layout';
import Dashboard from '@/bounty/Dashboard';
import CommandCenter from '@/bounty/CommandCenter';
import Opportunities from '@/bounty/Opportunities';
import Jobs, { JobDetail } from '@/bounty/Jobs';
import Finance from '@/bounty/Finance';
import Operations from '@/bounty/Operations';
import SettingsPage from '@/bounty/Settings';
import '@/bounty/command.css';

// REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH

function AppRouter() {
  const location = useLocation();
  
  // Check URL fragment (not query params) for session_id - MUST be synchronous
  // This runs BEFORE ProtectedRoute to prevent race conditions
  if (location.hash?.includes('session_id=')) {
    return <AuthCallback />;
  }
  
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<CommandLayout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/dashboard" element={<Navigate to="/" replace />} />
        <Route path="/command-center" element={<CommandCenter />} />
        <Route path="/opportunities" element={<Opportunities />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/jobs/:jobId" element={<JobDetail />} />
        <Route path="/payments" element={<Finance paymentsOnly />} />
        <Route path="/finance" element={<Finance />} />
        {['repositories', 'agents', 'pull-requests', 'learning', 'security', 'audit-log'].map(page => <Route key={page} path={`/${page}`} element={<Operations page={page} />} />)}
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

function App() {
  return (
    <div className="App dark">
      <Toaster data-testid="global-toaster" richColors position="top-center" />
      <BrowserRouter>
        <AppRouter />
      </BrowserRouter>
    </div>
  );
}

export default App;
