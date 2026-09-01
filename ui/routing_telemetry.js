/* routing_telemetry.js - 后端遥测快照格式化；浏览器不直连 Mihomo Controller */
(() => {
  function safeBytes(value) {
    const number = Number(value);
    return Number.isFinite(number) && number > 0
      ? Math.min(Math.floor(number), Number.MAX_SAFE_INTEGER) : 0;
  }

  function formatRate(value) {
    const bytes = safeBytes(value);
    if (bytes < 1024) return `${bytes} B/s`;
    const units = ['KB/s', 'MB/s', 'GB/s', 'TB/s'];
    let scaled = bytes / 1024;
    let index = 0;
    while (scaled >= 1024 && index < units.length - 1) {
      scaled /= 1024;
      index += 1;
    }
    const digits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 2;
    return `${scaled.toFixed(digits)} ${units[index]}`;
  }

  function normalizeSnapshot(value) {
    const source = value || {};
    const phase = ['idle', 'connecting', 'connected', 'reconnecting']
      .includes(source.phase) ? source.phase : 'idle';
    return {
      phase,
      retrySeconds: Math.min(15, Math.max(0, Number(source.retry_seconds) || 0)),
      up: safeBytes(source.up),
      down: safeBytes(source.down),
      upTotal: safeBytes(source.up_total),
      downTotal: safeBytes(source.down_total),
      observability: source.observability || {},
    };
  }

  window.RoutingTelemetryTools = Object.freeze({
    safeBytes, formatRate, normalizeSnapshot,
  });
})();
