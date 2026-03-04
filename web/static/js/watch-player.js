/**
 * Watch Player - Video player with controls
 * Handles video playback, progress, buttons
 */

class WatchPlayer {
    constructor(videoElementId = 'video-player') {
        this.video = document.getElementById(videoElementId);
        this.init();
    }

    init() {
        if (!this.video) {
            console.error('Video element not found');
            return;
        }

        // Setup controls
        this.setupPlayerControls();
        this.setupProgressBar();
        this.setupVolumeControl();
        this.setupQualitySelector();
    }

    setupPlayerControls() {
        const playBtn = document.getElementById('play-btn');
        const pauseBtn = document.getElementById('pause-btn');
        const fullscreenBtn = document.getElementById('fullscreen-btn');

        playBtn?.addEventListener('click', () => this.video.play());
        pauseBtn?.addEventListener('click', () => this.video.pause());
        fullscreenBtn?.addEventListener('click', () => this.toggleFullscreen());
    }

    setupProgressBar() {
        const progressBar = document.getElementById('progress-bar');
        
        this.video?.addEventListener('timeupdate', () => {
            const percent = (this.video.currentTime / this.video.duration) * 100;
            progressBar.style.width = percent + '%';
            document.getElementById('current-time').textContent = this.formatTime(this.video.currentTime);
        });

        // Seek
        progressBar?.parentElement?.addEventListener('click', (e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            const percent = (e.clientX - rect.left) / rect.width;
            this.video.currentTime = percent * this.video.duration;
        });
    }

    setupVolumeControl() {
        const volumeSlider = document.getElementById('volume-slider');
        
        volumeSlider?.addEventListener('input', (e) => {
            this.video.volume = e.target.value / 100;
        });
    }

    setupQualitySelector() {
        const qualitySelector = document.getElementById('quality-selector');
        
        qualitySelector?.addEventListener('change', (e) => {
            this.changeQuality(e.target.value);
        });
    }

    toggleFullscreen() {
        if (!document.fullscreenElement) {
            document.documentElement.requestFullscreen();
        } else {
            document.exitFullscreen();
        }
    }

    changeQuality(quality) {
        const currentTime = this.video.currentTime;
        // Would switch video source
        this.video.currentTime = currentTime;
    }

    formatTime(seconds) {
        const hrs = Math.floor(seconds / 3600);
        const mins = Math.floor((seconds % 3600) / 60);
        const secs = Math.floor(seconds % 60);
        return `${hrs}:${mins}:${secs}`.replace(/\b\d\b/g, '0$&');
    }

    downloadFile() {
        const link = document.createElement('a');
        link.href = this.video.src;
        link.download = 'video.mp4';
        link.click();
    }

    shareFile() {
        const url = window.location.href;
        if (navigator.share) {
            navigator.share({
                title: 'FileBot - Watch',
                url: url
            });
        } else {
            prompt('Share link:', url);
        }
    }
}

// Initialize player on page load
document.addEventListener('DOMContentLoaded', () => {
    new WatchPlayer();
});
