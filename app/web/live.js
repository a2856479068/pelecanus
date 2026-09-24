// Both pages render fresh server snapshots. Cross-tab messages only invalidate;
// they never carry a guessed state or replace a response from the server.
window.PelicanLive = (() => {
  let snapshot, connected = false, receivedAt = 0, revision = 0;
  let pending, controller, timer, channel;
  const listeners = [];
  try { channel = new BroadcastChannel('pelican-state-changed'); } catch {}
  const fresh = () => connected && performance.now() - receivedAt < 4000;
  async function refresh() {
    if (pending || !listeners.length) return pending;
    clearTimeout(timer);
    const version = revision;
    controller = new AbortController();
    const abort = setTimeout(() => controller.abort(), 3000);
    pending = (async () => {
      try {
        const response = await fetch('/api/state', {cache:'no-store', credentials:'same-origin', signal:controller.signal});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const value = await response.json();
        if (version !== revision) return;
        if (!Number.isFinite(value.server_time) || !value.settings) throw new Error('无效状态');
        snapshot = value; connected = true; receivedAt = performance.now();
        listeners.forEach(listener => listener.state(value));
      } catch (error) {
        if (version === revision) {
          connected = false;
          listeners.forEach(listener => listener.error(error));
        }
      } finally {
        clearTimeout(abort);
        pending = null;
        timer = setTimeout(refresh, version === revision ? 1000 : 0);
      }
    })();
    return pending;
  }
  function invalidate(broadcast = true) {
    revision++;
    controller?.abort();
    if (broadcast) channel?.postMessage('refresh');
    void refresh();
  }
  if (channel) channel.onmessage = () => invalidate(false);
  addEventListener('focus', () => invalidate(false));
  addEventListener('pageshow', () => invalidate(false));
  addEventListener('visibilitychange', () => { if (!document.hidden) invalidate(false); });
  return {
    start(state, error) { listeners.push({state, error}); void refresh(); },
    invalidate,
    fresh,
    // Live elapsed times use the time actually returned by the server. Never
    // keep advancing a fictional running timer after the connection is lost.
    now: () => fresh() ? snapshot.server_time : null,
    interval(seconds) {
      return seconds >= 60 && seconds % 60 === 0 ? `${seconds / 60} 分钟（${seconds} 秒）` : `${seconds} 秒`;
    }
  };
})();
