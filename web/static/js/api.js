/**
 * API Helper - Handles all API calls
 * - Admin APIs (JWT via auth_token)
 * - User APIs (My Files via cookie/session)
 * - Public config
 */

class API {
    constructor() {
        this.adminBaseUrl = '/api/admin';
        this.userBaseUrl = '/api/user';
        this.publicBaseUrl = '/api/public';
        this.token = localStorage.getItem('filebot_token'); // Fixed key
    }

    /**
     * Generic admin API request (JWT)
     */
    async adminRequest(endpoint, method = 'GET', data = null) {
        try {
            const options = {
                method,
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${this.token}`
                }
            };
            if (data) {
                options.body = JSON.stringify(data);
            }

            const response = await fetch(`${this.adminBaseUrl}${endpoint}`, options);
            
            // Handle Session Expiry (401)
            if (response.status === 401) {
                localStorage.removeItem('filebot_token');
                window.location.href = '/login.html';
                throw new Error('Session expired');
            }

            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.detail || 'API request failed');
            }
            return result;
        } catch (error) {
            console.error(`❌ Admin API error [${endpoint}]:`, error);
            throw error;
        }
    }

    /**
     * Generic user API request (cookies/session)
     */
    async userRequest(endpoint, method = 'GET', data = null) {
        try {
            const options = {
                method,
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include'
            };
            if (data) {
                options.body = JSON.stringify(data);
            }

            const response = await fetch(`${this.userBaseUrl}${endpoint}`, options);
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.detail || 'User API request failed');
            }
            return result;
        } catch (error) {
            console.error(`❌ User API error [${endpoint}]:`, error);
            throw error;
        }
    }

    /**
     * Generic public API request
     */
    async publicRequest(endpoint, method = 'GET') {
        try {
            const response = await fetch(`${this.publicBaseUrl}${endpoint}`, { method });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.detail || 'Public API request failed');
            }
            return result;
        } catch (error) {
            console.error(`❌ Public API error [${endpoint}]:`, error);
            throw error;
        }
    }

    // =========================
    // Admin convenience methods
    // =========================

    async getDashboard() {
        // Backend no longer needs ?token= in query, JWT is in header
        return this.adminRequest('/dashboard');
    }

    async getUsers(skip = 0, limit = 10) {
        return this.adminRequest(`/users?skip=${skip}&limit=${limit}`);
    }

    async banUser(userId, reason) {
        return this.adminRequest(`/users/${userId}/ban`, 'POST', {
            user_id: userId,
            reason
        });
    }

    async unbanUser(userId) {
        return this.adminRequest(`/users/${userId}/unban`, 'POST');
    }

    async upgradeUser(userId) {
        return this.adminRequest(`/users/${userId}/upgrade`, 'POST');
    }

    async getConfig() {
        return this.adminRequest('/config');
    }

    async updateConfig(config) {
        return this.adminRequest('/config', 'POST', config);
    }

    async saveSiteConfig(config) {
        return this.adminRequest('/site-config', 'POST', config);
    }

    // =========================
    // User/My Files methods
    // =========================

    async getMyFiles() {
        return this.userRequest('/myfiles');
    }

    async deleteUserFile(fileId) {
        return this.userRequest(`/files/${fileId}`, 'DELETE');
    }

    // =========================
    // Public config
    // =========================

    async getPublicConfig() {
        return this.publicRequest('/config');
    }
}

// Global API instance
const api = new API();
