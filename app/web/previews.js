// Render full HTML documents on a desktop-size canvas, then fit that canvas
// into each thumbnail/detail pane. The iframe keeps its opaque sandbox.
(() => {
  const frames = new Map();
  function fit(frame, size) {
    const host = frame.parentElement;
    const scale = Math.min(host.clientWidth / size.width, host.clientHeight / size.height);
    frame.style.width = `${size.width}px`;
    frame.style.height = `${size.height}px`;
    frame.style.transform = `translate(${(host.clientWidth - size.width * scale) / 2}px, ${(host.clientHeight - size.height * scale) / 2}px) scale(${scale})`;
  }
  function sync() {
    for (const [frame, size] of frames) {
      if (!frame.isConnected) { size.observer.disconnect(); frames.delete(frame); }
    }
    document.querySelectorAll('iframe[data-fit-preview]').forEach(frame => {
      if (frames.has(frame)) return;
      const size = {width:1000, height:750};
      size.observer = new ResizeObserver(() => fit(frame, size));
      frames.set(frame, size);
      size.observer.observe(frame.parentElement);
      fit(frame, size);
    });
  }
  addEventListener('message', event => {
    if (event.data?.type !== 'pelican:preview-size') return;
    for (const [frame, size] of frames) {
      if (event.source !== frame.contentWindow) continue;
      const {width, height} = event.data;
      if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return;
      size.width = Math.max(1000, Math.min(8192, width));
      size.height = Math.max(750, Math.min(8192, height));
      fit(frame, size);
      return;
    }
  });
  new MutationObserver(sync).observe(document.documentElement, {childList:true, subtree:true});
  sync();
})();
