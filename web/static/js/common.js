// common.js - Shared UI Logic for FileBot Web Layer

const token = localStorage.getItem('filebot_token');
if (!token && !window.location.pathname.includes('login.html')) {
    window.location.href = '/login.html';
}

function doLogout() {
    localStorage.removeItem('filebot_token');
    window.location.href = '/login.html';
}

async function apiFetch(path, opts = {}) {
    const r = await fetch(path, {
        ...opts,
        headers: {
            'Authorization': 'Bearer ' + token,
            ...(opts.headers || {})
        }
    });
    // If auth fails anywhere, nuke token and bounce
    if (r.status === 401 && !path.includes('/login')) {
        doLogout();
    }
    return r;
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebar-backdrop');
    const btn = document.getElementById('hamburger-btn');
    
    if (sidebar) sidebar.classList.toggle('open');
    if (backdrop) backdrop.classList.toggle('show');
    if (btn) btn.classList.toggle('open');
}

document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
        const sidebar = document.getElementById('sidebar');
        if (sidebar && sidebar.classList.contains('open')) toggleSidebar();
    }
});

// Sidebar injection
async function injectSidebar(activePage) {
    const layout = document.querySelector('.layout');
    if (!layout) return; // Not an app page

    let isAdmin = false;
    try {
        const r = await apiFetch('/api/admin/dashboard');
        if (r.status === 200) isAdmin = true;
    } catch (e) {}

    // First, inject hamburger and backdrop to body
    let bodyHtml = `
        <button class="hamburger" id="hamburger-btn" aria-label="Open menu" onclick="toggleSidebar()">
            <span></span><span></span><span></span>
        </button>
        <div class="sidebar-backdrop" id="sidebar-backdrop" onclick="toggleSidebar()"></div>
    `;
    document.body.insertAdjacentHTML('afterbegin', bodyHtml);

    // Then inject sidebar to layout
    let html = `
        <nav class="sidebar" id="sidebar">
            <a href="/dashboard.html" class="brand">
                <span>⚡</span>
                <div class="brand-text">FileBot</div>
            </a>
            <ul class="nav-menu">
    `;

    // USER LINKS
    html += `
                <li class="nav-item">
                    <a href="/dashboard.html" class="nav-link ${activePage === 'dashboard' ? 'active' : ''}">
                        <span class="nav-icon">📊</span><span class="nav-text">Dashboard</span>
                    </a>
                </li>
                <li class="nav-item">
                    <a href="/myfiles.html" class="nav-link ${activePage === 'myfiles' ? 'active' : ''}">
                        <span class="nav-icon">📂</span><span class="nav-text">My Files</span>
                    </a>
                </li>
                <li class="nav-item">
                    <a href="/settings.html" class="nav-link ${activePage === 'settings' ? 'active' : ''}">
                        <span class="nav-icon">⚙️</span><span class="nav-text">Settings</span>
                    </a>
                </li>
    `;

    // ADMIN LINKS
    if (isAdmin) {
        if (activePage === 'admin') {
            html += `
                <div style="margin: 20px 0 10px 0; color: #8b949e; font-size: 12px; text-transform: uppercase;">Admin Tools</div>
                <div class="nav-link active" onclick="switchTab('overview', this)"><span class="nav-icon">📊</span><span class="nav-text">Overview</span></div>
                <div class="nav-link" onclick="switchTab('global-config', this)"><span class="nav-icon">⚙️</span><span class="nav-text">Global Config</span></div>
                <div class="nav-link" onclick="switchTab('bot-plans', this)"><span class="nav-icon">💎</span><span class="nav-text">Bot Plans</span></div>
                <div class="nav-link" onclick="switchTab('broadcast', this)"><span class="nav-icon">📢</span><span class="nav-text">Broadcasts</span></div>
            `;
        } else {
            html += `
                <li class="nav-item" style="margin-top: 15px; padding-top: 15px; border-top: 1px solid rgba(255,255,255,0.05);">
                    <a href="/admin.html" class="nav-link" style="color:#ff4444;">
                        <span class="nav-icon">🛡️</span><span class="nav-text">Admin Panel</span>
                    </a>
                </li>
            `;
        }
    }

    html += `
            </ul>
            <button onclick="doLogout()" class="nav-link" style="background: none; border: none; width: 100%; color: #ff7b72; margin-top: auto; cursor: pointer;">
                <span class="nav-icon">🚪</span>
                <span class="nav-text">Logout</span>
            </button>
        </nav>
    `;

    layout.insertAdjacentHTML('afterbegin', html);
}
