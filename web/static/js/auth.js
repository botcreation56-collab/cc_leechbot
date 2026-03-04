/**
 * Authentication Flow
 * Handles login with one-time codes for:
 * - Admin panel
 * - My Files (user side)
 */

class Auth {
    constructor() {
        this.adminApiUrl = '/auth';
        this.userApiUrl = '/api/user';
        this.token = localStorage.getItem('filebot_token');
    }

    /**
     * Request one-time code for admin
     */
    async requestAdminCode(userId) {
        try {
            const response = await fetch(`${this.adminApiUrl}/request-code`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
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
     * Verify admin code and get JWT token
     */
    async verifyAdminCode(userId, code) {
        try {
            const response = await fetch(`${this.adminApiUrl}/verify-code`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    user_id: parseInt(userId, 10),
                    code: (code || '').trim()
                })
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.detail || 'Admin verification failed');
            }

            // Save token for admin APIs
            localStorage.setItem('filebot_token', data.token);
            this.token = data.token;
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
            const response = await fetch(`/api/auth/request-login-link`, {  // FIX: Changed from /api/user to /api/auth
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
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
     * Verify Magic Token and create session
     */
    async verifyMagicToken(token) {
        try {
            const response = await fetch(`/api/auth/verify-magic-token`, {  // FIX: Changed from /api/user to /api/auth
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include',
                body: JSON.stringify({ token: token })
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.detail || 'Link verification failed');
            }

            // FIX: Store session token for authenticated requests
            if (data.token) {
                localStorage.setItem('filebot_token', data.token);
                this.token = data.token;
            }

            return data;
        } catch (error) {
            console.error('❌ Verify Magic Link error:', error);
            throw error;
        }
    }

    /**
     * Verify OTP for My Files and create session (usually via cookie)
     * @deprecated Kept for backward compatibility or admin flows if needed
     */
    async verifyMyFilesOtp(userId, code) {
        // ... (existing logic if needed, but we are moving away from OTP)
        throw new Error("OTP verification is deprecated.");
    }

    /**
     * Check if admin is authenticated
     */
    isAuthenticated() {
        return !!this.token;
    }

    /**
     * Logout admin
     */
    logout() {
        localStorage.removeItem('filebot_token');
        this.token = null;
        window.location.href = '/login.html';
    }

    /**
     * Get authorization header for admin APIs
     */
    getAuthHeader() {
        return {
            'Authorization': `Bearer ${this.token}`,
            'Content-Type': 'application/json'
        };
    }
}

// Global auth instance
const auth = new Auth();

// Page wiring
document.addEventListener('DOMContentLoaded', () => {
    const path = window.location.pathname;

    // Check for Magic Token in URL
    const urlParams = new URLSearchParams(window.location.search);
    const magicToken = urlParams.get('magic_token');

    if (magicToken) {
        handleMagicLogin(magicToken);
    }

    // Admin pages (protect if not login.html or public)
    if (!path.includes('login') && path.startsWith('/dashboard')) {
        if (!auth.isAuthenticated()) {
            window.location.href = '/login.html';
            return;
        }
    }

    // Wire login.html elements if present
    const adminUserIdInput = document.getElementById('admin-user-id');
    const adminRequestCodeBtn = document.getElementById('admin-request-code');
    const adminOtpInput = document.getElementById('admin-otp');
    const adminVerifyBtn = document.getElementById('admin-verify');
    const adminMessage = document.getElementById('admin-login-message');

    const myFilesUserIdInput = document.getElementById('myfiles-user-id');
    const myFilesRequestOtpBtn = document.getElementById('myfiles-request-otp');
    // myFilesOtpInput removed
    // myFilesVerifyBtn removed
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
});

async function handleMagicLogin(token) {
    // Show a loading overlay or simple message if possible, strictly referencing UI elements that exist
    // But we are likely on login.html or index.html

    // Attempt verification
    try {
        // If we are on login page, show status
        const msgEl = document.getElementById('myfiles-login-message');
        if (msgEl) msgEl.innerText = "Verifying Magic Link...";

        const data = await auth.verifyMagicToken(token);

        if (msgEl) msgEl.innerText = "Success! Redirecting...";

        // Redirect to dashboard (consistent path)
        setTimeout(() => {
            window.location.href = '/dashboard.html';
        }, 500);
    } catch (e) {
        console.error(e);
        const msgEl = document.getElementById('myfiles-login-message') || document.body;
        if (msgEl.innerText !== undefined) msgEl.innerText = "Login failed: " + e.message;
        else alert("Login failed: " + e.message);
    }
}
