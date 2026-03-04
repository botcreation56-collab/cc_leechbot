/**
 * Dashboard UI Logic
 */

class Dashboard {
    constructor() {
        this.api = api;
        this.init();
    }

    async init() {
        try {
            await this.loadDashboard();
            this.setupEventListeners();
        } catch (error) {
            this.showError('Failed to load dashboard: ' + error.message);
        }
    }

    /**
     * Load dashboard data
     */
    async loadDashboard() {
        try {
            const stats = await this.api.getDashboard();
            this.updateDashboard(stats);
        } catch (error) {
            console.error('❌ Load dashboard error:', error);
            throw error;
        }
    }

    /**
     * Update dashboard UI with data
     * Expect backend to return:
     * {
     *   total_users, active_tasks, today_processed,
     *   success_count, failed_count,
     *   storage_usage, recent_tasks[], recent_errors[]
     * }
     */
    updateDashboard(stats) {
        // Summary cards
        document.getElementById('stat-total-users').textContent =
            stats.total_users ?? '-';
        document.getElementById('stat-active-tasks').textContent =
            stats.active_tasks ?? '-';
        document.getElementById('stat-today-processed').textContent =
            stats.today_processed ?? '-';
        document.getElementById('stat-success-failed').textContent =
            `${stats.success_count ?? 0} / ${stats.failed_count ?? 0}`;
        document.getElementById('stat-storage-usage').textContent =
            stats.storage_usage ?? '-';

        // Recent tasks table
        const tasksBody = document.getElementById('recent-tasks-tbody');
        if (tasksBody) {
            tasksBody.innerHTML = '';
            (stats.recent_tasks || []).forEach(task => {
                const tr = document.createElement('tr');

                const tdUser = document.createElement('td');
                tdUser.textContent = task.user || '-';
                tr.appendChild(tdUser);

                const tdFile = document.createElement('td');
                tdFile.textContent = task.file_name || '-';
                tr.appendChild(tdFile);

                const tdStatus = document.createElement('td');
                tdStatus.textContent = task.status || '-';
                tr.appendChild(tdStatus);

                const tdSize = document.createElement('td');
                tdSize.textContent = task.size_human || task.size || '-';
                tr.appendChild(tdSize);

                const tdTime = document.createElement('td');
                tdTime.textContent = task.created_at || '-';
                tr.appendChild(tdTime);

                tasksBody.appendChild(tr);
            });
        }

        // Recent errors
        const errorsList = document.getElementById('recent-errors-list');
        if (errorsList) {
            errorsList.innerHTML = '';
            (stats.recent_errors || []).forEach(err => {
                const li = document.createElement('li');
                li.textContent = `[${err.time || ''}] ${err.message || ''}`;
                errorsList.appendChild(li);
            });
        }
    }

    /**
     * Setup event listeners
     */
    setupEventListeners() {
        // Example admin actions, adjust IDs to your HTML if you keep them
        const logoutBtn = document.getElementById('logout-btn');
        if (logoutBtn) {
            logoutBtn.addEventListener('click', () => auth.logout());
        }

        // You can add more buttons (ban, unban, upgrade) mapped to modal or prompts
        const banBtn = document.getElementById('ban-btn');
        if (banBtn) {
            banBtn.addEventListener('click', () => this.openBanModal());
        }

        const unbanBtn = document.getElementById('unban-btn');
        if (unbanBtn) {
            unbanBtn.addEventListener('click', () => this.openUnbanModal());
        }

        const upgradeBtn = document.getElementById('upgrade-btn');
        if (upgradeBtn) {
            upgradeBtn.addEventListener('click', () => this.openUpgradeModal());
        }
    }

    openBanModal() {
        const userId = prompt('Enter user ID to ban:');
        if (userId) {
            const reason = prompt('Ban reason:');
            if (reason) {
                this.banUser(userId, reason);
            }
        }
    }

    openUnbanModal() {
        const userId = prompt('Enter user ID to unban:');
        if (userId) {
            this.unbanUser(userId);
        }
    }

    openUpgradeModal() {
        const userId = prompt('Enter user ID to upgrade to pro:');
        if (userId) {
            this.upgradeUser(userId);
        }
    }

    async banUser(userId, reason) {
        try {
            await this.api.banUser(userId, reason);
            this.showSuccess(`User ${userId} banned successfully`);
            this.loadDashboard();
        } catch (error) {
            this.showError('Failed to ban user: ' + error.message);
        }
    }

    async unbanUser(userId) {
        try {
            await this.api.unbanUser(userId);
            this.showSuccess(`User ${userId} unbanned successfully`);
            this.loadDashboard();
        } catch (error) {
            this.showError('Failed to unban user: ' + error.message);
        }
    }

    async upgradeUser(userId) {
        try {
            await this.api.upgradeUser(userId);
            this.showSuccess(`User ${userId} upgraded successfully`);
            this.loadDashboard();
        } catch (error) {
            this.showError('Failed to upgrade user: ' + error.message);
        }
    }

    showSuccess(message) {
        const alert = document.createElement('div');
        alert.className = 'alert alert-success';
        alert.textContent = message;
        document.body.insertBefore(alert, document.body.firstChild);
        setTimeout(() => alert.remove(), 3000);
    }

    showError(message) {
        const alert = document.createElement('div');
        alert.className = 'alert alert-error';
        alert.textContent = message;
        document.body.insertBefore(alert, document.body.firstChild);
        setTimeout(() => alert.remove(), 3000);
    }
}

// Initialize dashboard when page loads
document.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('section-overview')) {
        new Dashboard();
    }
});
