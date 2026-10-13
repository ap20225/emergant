# Preserved Google authentication testing

Use the existing owner and a current MongoDB session documented in memory/test_credentials.md. Do not replace the owner, run a new seed, or change identities.

1. Read MONGO_URL and DB_NAME from backend/.env. Query instance_config instance_owner, then users/user_sessions by user_id. Exclude Mongo _id when serializing.
2. Use current unexpired session_token as Secure, HttpOnly, SameSite=None cookie for the actual REACT_APP_BACKEND_URL hostname. Never print the token or place it in artifacts.
3. /api/auth/me must identify the owner; /api/bounty/bootstrap must return preview=false.
4. Test actual browser mutations, not only bearer curl. Include Origin in HTTP contract tests. Trusted first-party origin must pass, unrelated origin must fail.
5. Validate navigation and owner actions without redirect loops; callback remains synchronous useLocation().hash detection, one-time session exchange and dynamic window.location.origin redirect.
6. Verify unauthenticated requests cannot mutate or see private operational records; wrong-owner requests fail403.
7. No passwords are created for Google auth. Save any test identity changes in memory/test_credentials.md, excluding tokens/secrets from committed source.

No new account/session needed for current tests; existing active session is reused.