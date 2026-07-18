"""Compatibility facade for the local browser backend."""

from utils.browser_backend import (
    DAEMON_SCRIPT,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_RENDER_TIMEOUT,
    PUPPETEER_CONFIG,
    PUPPETEER_MODULE,
    BrowserDaemon,
    close_browser_daemon,
    render_with_browser_daemon,
    shutdown_browser_daemon,
)

__all__ = [
    "BrowserDaemon",
    "close_browser_daemon",
    "render_with_browser_daemon",
    "shutdown_browser_daemon",
]
