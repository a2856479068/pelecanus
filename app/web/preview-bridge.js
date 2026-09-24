// Runs inside the opaque, network-isolated generated document.
(() => {
  let pending = false, reports = 0, previous = '';
  function measure() {
    pending = false;
    const body = document.body, root = document.documentElement;
    if (!body || reports >= 4) return;
    let width = Math.max(innerWidth, root.scrollWidth, body.scrollWidth);
    let height = Math.max(innerHeight, root.scrollHeight, body.scrollHeight);
    // Include fixed-size artwork even when the document hides overflow.
    for (const element of document.querySelectorAll('body > *, svg')) {
      if (['SCRIPT', 'STYLE', 'LINK'].includes(element.tagName)) continue;
      const rect = element.getBoundingClientRect();
      width = Math.max(width, rect.width, rect.right + scrollX);
      height = Math.max(height, rect.height, rect.bottom + scrollY);
    }
    width = Math.ceil(width); height = Math.ceil(height);
    const signature = `${width}:${height}`;
    if (signature === previous) return;
    previous = signature; reports++;
    parent.postMessage({type:'pelican:preview-size', width, height}, '*');
  }
  function schedule() {
    if (!pending) { pending = true; requestAnimationFrame(measure); }
  }
  addEventListener('load', schedule);
  addEventListener('resize', schedule);
  new ResizeObserver(schedule).observe(document.documentElement);
  if (document.body) new ResizeObserver(schedule).observe(document.body);
  schedule();
})();
