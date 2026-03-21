/**
 * Authentication Flow - Secure Version
 * Uses httpOnly cookies for session storage and CSRF tokens for protection.
 */

class Auth {
    constructor() {
        this.adminApiUrl = '/auth';
        this.userApiUrl = '/api/user';
        this.csrfToken = null;
    }

    /**
     * Get CSRF token for requests
     */
    async getCsrfToken() {
        if (this.csrfToken) return this.csrfToken;
        
        try {
            const response = await fetch(`${this.adminApiUrl}/csrf-token`, {
                method: 'GET',
                credentials: 'include'
            });
            if (response.ok) {
                const data = await response.json();
                this.csrfToken = data.csrf_token;
                return this.csrfToken;
            }
        } catch (e) {
            console.error('Failed to get CSRF token:', e);
        }
        return null;
    }

    /**
     * Request one-time code for admin
     */
    async requestAdminCode(userId) {
        try {
            const csrf = await this.getCsrfToken();
            const response = await fetch(`${this.adminApiUrl}/request-code`, {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrf || ''
                },
                credentials: 'include',
                body: JSON.stringify({ user_id: parseInt(userId, 10) })
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.detail || 'Failed to request admin code');
            }
            return data;
        } catch (error) {
            console.error('❌ Request admin code error:', error);
            throw error;
        }
    }

    /**
     * Verify admin code and get JWT token (stored in httpOnly cookie)
     */
    async verifyAdminCode(userId, code) {
        try {
            const csrf = await this.getCsrfToken();
            const response = await fetch(`${this.adminApiUrl}/verify-code`, {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrf || ''
                },
                credentials: 'include',
                body: JSON.stringify({
                    user_id: parseInt(userId, 10),
                    code: (code || '').trim()
                })
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.detail || 'Admin verification failed');
            }

            // Session token is now in httpOnly cookie (set by server)
            // We store user_id in localStorage for convenience
            localStorage.setItem('filebot_user_id', data.user_id);
            this.csrfToken = data.csrf_token;
            
            return data;
        } catch (error) {
            console.error('❌ Verify admin code error:', error);
            throw error;
        }
    }

    /**
     * Request Magic Link for My Files (user side)
     */
    async requestMyFilesOtp(userId) {
        try {
            const csrf = await this.getCsrfToken();
            const response = await fetch(`/api/auth/request-login-link`, {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': csrf || ''
                },
                credentials: 'include',
                body: JSON.stringify({ user_id: parseInt(userId, 10) })
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.detail || 'Failed to request magic link');
            }
            return data;
        } catch (error) {
            console.error('❌ Request Magic Link error:', error);
            throw error;
        }
    }

    /**
     * Verify Magic Token and create session (uses httpOnly cookie)
     */
    async verifyMagicToken(token) {
        try {
            const response = await fetch(`/api/auth/verify-magic-token`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',  // Important: send cookies
                body: JSON.stringify({ token: token })
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.detail || 'Link verification failed');
            }

            // Session stored in httpOnly cookie by server
            localStorage.setItem('filebot_user_id', data.user_id);
            this.csrfToken = data.csrf_token;

            return data;
        } catch (error) {
            console.error('❌ Verify Magic Link error:', error);
            throw error;
        }
    }

    /**
     * Check if user is authenticated (has valid session cookie)
     */
    async isAuthenticated() {
        try {
            // Check with server if session is valid
            const csrf = await this.getCsrfToken();
            const response = await fetch(`${this.adminApiUrl}/csrf-token`, {
                method: 'GET',
                credentials: 'include',
                headers: { 'X-CSRF-Token': csrf || '' }
            });
            return response.ok;
        } catch (e) {
            return false;
        }
    }

    /**
     * Logout user
     */
    async logout() {
        try {
            const csrf = await this.getCsrfToken();
            await fetch(`${this.adminApiUrl}/logout`, {
                method: 'POST',
                headers: { 'X-CSRF-Token': csrf || '' },
                credentials: 'include'
            });
        } catch (e) {
            console.error('Logout error:', e);
        }
        localStorage.removeItem('filebot_token');
        localStorage.removeItem('filebot_user_id');
        this.csrfToken = null;
        window.location.href = '/login.html';
    }

    /**
     * Get authorization header for API calls (fallback for non-browser clients)
     * Prefer using cookies for browser requests
     */
    getAuthHeader() {
        const token = localStorage.getItem('filebot_token');
        if (token) {
            return {
                'Authorization': `Bearer ${token}`,
                'Content-Type': 'application/json',
                'X-CSRF-Token': this.csrfToken || ''
            };
        }
        return {
            'Content-Type': 'application/json',
            'X-CSRF-Token': this.csrfToken || ''
        };
    }

    /**
     * Make authenticated API request
     */
    async apiRequest(url, options = {}) {
        const csrf = await this.getCsrfToken();
        const defaultOptions = {
            credentials: 'include',  // Send cookies
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrf || ''
            }
        };
        return fetch(url, { ...defaultOptions, ...options });
    }
}

// Global auth instance
const auth = new Auth();

// Page wiring
document.addEventListener('DOMContentLoaded', () => {
    const path = window.location.pathname;

    // Check for Token Signature in URL (secure magic login)
    const urlParams = new URLSearchParams(window.location.search);
    const tokenSig = urlParams.get('token_sig');

    if (tokenSig) {
        // Use signature-based login (doesn't expose actual token in URL)
        handleMagicLogin(tokenSig);
        return;
    }

    // Also support legacy token format for backwards compat
    const magicToken = urlParams.get('magic_token');
    if (magicToken) {
        handleMagicLogin(magicToken);
        return;
    }

    // Admin pages protection
    if (!path.includes('login') && path.startsWith('/dashboard')) {
        checkAuthAndRedirect();
    }

    // Wire login.html elements if present
    wireLoginPage();
});

async function checkAuthAndRedirect() {
    const isAuth = await auth.isAuthenticated();
    if (!isAuth) {
        window.location.href = '/login.html';
    }
}

function wireLoginPage() {
    const adminUserIdInput = document.getElementById('admin-user-id');
    const adminRequestCodeBtn = document.getElementById('admin-request-code');
    const adminOtpInput = document.getElementById('admin-otp');
    const adminVerifyBtn = document.getElementById('admin-verify');
    const adminMessage = document.getElementById('admin-login-message');

    const myFilesUserIdInput = document.getElementById('myfiles-user-id');
    const myFilesRequestOtpBtn = document.getElementById('myfiles-request-otp');
    const myFilesMessage = document.getElementById('myfiles-login-message');

    // Admin login events
    if (adminRequestCodeBtn && adminUserIdInput && adminMessage) {
        adminRequestCodeBtn.addEventListener('click', async () => {
            const userId = (adminUserIdInput.value || '').trim();
            if (!userId) {
                adminMessage.textContent = 'Enter your Telegram User ID.';
                return;
            }
            adminMessage.textContent = 'Requesting code...';
            try {
                await auth.requestAdminCode(userId);
                adminMessage.textContent = 'Code sent to your Telegram bot. Check your chat.';
            } catch (e) {
                adminMessage.textContent = e.message || 'Failed to request code.';
            }
        });
    }

    if (adminVerifyBtn && adminUserIdInput && adminOtpInput && adminMessage) {
        adminVerifyBtn.addEventListener('click', async () => {
            const userId = (adminUserIdInput.value || '').trim();
            const code = (adminOtpInput.value || '').trim();
            if (!userId || !code) {
                adminMessage.textContent = 'Enter User ID and code.';
                return;
            }
            adminMessage.textContent = 'Verifying...';
            try {
                await auth.verifyAdminCode(userId, code);
                adminMessage.textContent = 'Login successful. Redirecting...';
                window.location.href = '/dashboard.html';
            } catch (e) {
                adminMessage.textContent = e.message || 'Verification failed.';
            }
        });
    }

    // My Files login events
    if (myFilesRequestOtpBtn && myFilesUserIdInput && myFilesMessage) {
        myFilesRequestOtpBtn.addEventListener('click', async () => {
            const userId = (myFilesUserIdInput.value || '').trim();
            if (!userId) {
                myFilesMessage.textContent = 'Enter your Telegram User ID.';
                return;
            }
            myFilesMessage.textContent = 'Sending Magic Link...';
            try {
                await auth.requestMyFilesOtp(userId);
                myFilesMessage.innerHTML = '✨ <b>Link sent!</b> Check your Telegram bot messages.';
                myFilesRequestOtpBtn.disabled = true;
                myFilesRequestOtpBtn.textContent = 'Link Sent';
            } catch (e) {
                myFilesMessage.textContent = e.message || 'Failed to send link.';
            }
        });
    }
}

async function handleMagicLogin(token) {
    const msgEl = document.getElementById('myfiles-login-message') || document.getElementById('admin-login-message');
    if (msgEl) msgEl.innerText = "Verifying...";

    try {
        const data = await auth.verifyMagicToken(token);
        if (msgEl) msgEl.innerText = "Success! Redirecting...";
        
        // Clear URL parameters for security
        window.history.replaceState({}, document.title, window.location.pathname);
        
        setTimeout(() => {
            window.location.href = '/dashboard.html';
        }, 500);
    } catch (e) {
        console.error(e);
        if (msgEl) msgEl.innerText = "Login failed: " + (e.message || 'Unknown error');
        // Also clear URL on error
        window.history.replaceState({}, document.title, window.location.pathname);
    }
}
