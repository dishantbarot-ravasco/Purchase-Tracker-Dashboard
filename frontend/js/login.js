// frontend/js/login.js — drives login.html: password step -> optional
// email-OTP device-verify step -> redirect to home.html. Also picks up
// the Google OAuth redirect's query params (?oauth_ready=1 / ?step=device_verify
// / ?oauth_error=...). No imports - plain script tag.

const passwordStep = document.getElementById('passwordStep');
const otpStep = document.getElementById('otpStep');
const errorBox = document.getElementById('loginError');

// "Remember me" hands the credential to the BROWSER's own password manager
// via the standard Credential Management API, rather than caching it
// ourselves - unlike the TDS Automation App's old (removed) approach of
// storing an "encrypted" password in localStorage with the decryption key
// sitting right next to it (readable by anything that can read
// localStorage, e.g. an XSS bug), navigator.credentials.store() puts the
// password only in the browser's own encrypted vault, never anywhere this
// page's JS can read it back. Supported by Chrome/Edge; on browsers without
// it (Firefox, Safari) this quietly does nothing extra and the fields fall
// back to each browser's own native autofill from the `autocomplete`/`name`
// attributes on the inputs. Device trust itself is unconditional either way
// - the pt_device httpOnly cookie (apps/services/device_service.py) is set
// after OTP verification regardless of this checkbox, and lasts the same
// 365 days as TDS's own tds_device cookie.
const supportsCredentialAPI = () => 'PasswordCredential' in window && 'credentials' in navigator;

async function storeCredentialIfRemembered(email, password) {
  if (!document.getElementById('rememberMe').checked || !supportsCredentialAPI()) return;
  try {
    const cred = new PasswordCredential({ id: email, password, name: email });
    await navigator.credentials.store(cred);
  } catch (_) {
    // Best-effort only (e.g. browser declined) - never block sign-in on this.
  }
}

// Offer to sign in automatically from a credential the browser already has
// saved for this site, so a returning user doesn't have to retype email or
// password at all - just approve the browser's own account-chooser prompt,
// if it shows one (mediation:'optional' silently resolves instead of
// prompting when there's exactly one match and the user hasn't previously
// dismissed it).
(async function trySilentSignIn() {
  if (!supportsCredentialAPI()) return;
  try {
    const cred = await navigator.credentials.get({ password: true, mediation: 'optional' });
    if (cred && cred.type === 'password' && cred.password) {
      document.getElementById('email').value = cred.id;
      document.getElementById('password').value = cred.password;
      document.getElementById('rememberMe').checked = true;
      passwordStep.requestSubmit();
    }
  } catch (_) {
    // Ignored - falls back to a normal manual sign-in.
  }
})();

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.add('show');
}
function clearError() {
  errorBox.textContent = '';
  errorBox.classList.remove('show');
}
function showOtpStep() {
  passwordStep.style.display = 'none';
  otpStep.style.display = '';
  document.getElementById('cardTitle').textContent = 'Verify your device';
  document.getElementById('cardSub').textContent = 'Enter the code we emailed you';
  document.getElementById('otpCode').focus();
}
function showPasswordStep() {
  otpStep.style.display = 'none';
  passwordStep.style.display = '';
  document.getElementById('cardTitle').textContent = 'Sign In';
  document.getElementById('cardSub').textContent = 'Use your company account credentials';
  document.getElementById('otpCode').value = '';
}

async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' ? detail : (detail ? JSON.stringify(detail) : 'Something went wrong. Please try again.');
    throw new Error(message);
  }
  return data;
}

passwordStep.addEventListener('submit', async (e) => {
  e.preventDefault();
  clearError();
  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  const btn = document.getElementById('signInBtn');
  btn.disabled = true;
  try {
    const data = await postJson('/api/auth/login', { email, password });
    if (data.status === 'ok') {
      await storeCredentialIfRemembered(email, password);
      window.location.href = '/home.html';
    } else if (data.status === 'device_verify') {
      showOtpStep();
    } else {
      showError('Unexpected response from server.');
    }
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
  }
});

otpStep.addEventListener('submit', async (e) => {
  e.preventDefault();
  clearError();
  const code = document.getElementById('otpCode').value.trim();
  const btn = document.getElementById('verifyBtn');
  btn.disabled = true;
  try {
    const data = await postJson('/api/auth/device-verify', { code });
    if (data.status === 'ok') {
      const email = document.getElementById('email').value.trim();
      const password = document.getElementById('password').value;
      await storeCredentialIfRemembered(email, password);
      window.location.href = '/home.html';
    } else {
      showError('Unexpected response from server.');
    }
  } catch (err) {
    showError(err.message);
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('backToLogin').addEventListener('click', () => {
  clearError();
  showPasswordStep();
});

// Recovery path for a lost/failed-to-arrive OTP email (see
// apps/services/device_service.py's send_device_otp() docstring - the send
// itself is backgrounded and best-effort, so a real delivery failure is
// logged server-side but has no way to reach the user in real time; this
// gives them a way to retry instead of being stuck on this screen with a
// code that never arrives and no path forward except abandoning sign-in).
document.getElementById('resendCode').addEventListener('click', async () => {
  clearError();
  const resendLink = document.getElementById('resendCode');
  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  resendLink.textContent = 'Sending...';
  try {
    const data = await postJson('/api/auth/login', { email, password });
    if (data.status === 'ok') {
      window.location.href = '/home.html';
      return;
    }
    resendLink.textContent = 'Code sent';
    setTimeout(() => { resendLink.textContent = 'Resend code'; }, 4000);
  } catch (err) {
    resendLink.textContent = 'Resend code';
    showError(err.message);
  }
});

// ── Google OAuth redirect handling ─────────────────────────────────────
const OAUTH_ERROR_MESSAGES = {
  cancelled: 'Sign-in was cancelled.',
  state_mismatch: 'Sign-in session expired. Please try again.',
  token_failed: 'Could not complete sign-in with Google. Please try again.',
  userinfo_failed: 'Could not fetch your Google account details. Please try again.',
  unverified_email: 'Your Google email is not verified.',
  domain_not_allowed: 'This Google account is outside the allowed organization.',
  not_registered: 'No Purchase Tracker account exists for this email. Contact an administrator.',
  email_failed: 'Could not send the verification email. Please try again or contact an administrator.',
};

(async function handleOAuthRedirect() {
  const params = new URLSearchParams(window.location.search);

  if (params.get('oauth_error')) {
    showError(OAUTH_ERROR_MESSAGES[params.get('oauth_error')] || 'Google sign-in failed. Please try again.');
    return;
  }

  if (params.get('step') === 'device_verify') {
    // google_callback already stashed pending_user_id in the session -
    // same OTP entry step a password login's new-device path uses.
    showOtpStep();
    return;
  }

  if (params.get('oauth_ready') === '1') {
    // google_callback already set the pt_access/pt_refresh cookies directly
    // on the redirect - this one-time pickup is just to consume/clear the
    // server-side session stash so it can't be replayed; failure here isn't
    // fatal since the cookies are already active either way.
    try {
      await fetch('/api/auth/google/session-token', { credentials: 'same-origin' });
    } catch (e) {
      // Logged, not swallowed - not fatal since the pt_access/pt_refresh
      // cookies were already set directly on the redirect that brought us
      // here (see google_callback in google_oauth_views.py), so sign-in
      // still succeeds, but a real failure here is worth being able to see.
      console.warn('google session-token pickup failed (non-fatal):', e);
    }
    window.location.href = '/home.html';
  }
})();
