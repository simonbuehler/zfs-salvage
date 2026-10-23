// Workflow step bar shared by all pages: 1 find a state -> 2 select files -> 3 transfer.
// Usage: <nav id="steps" data-page="status|select|transfer"></nav><script src="/steps.js"></script>
(function () {
  const css = `
  #steps{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
  #steps a{flex:1 1 200px;display:flex;gap:9px;align-items:center;text-decoration:none;color:var(--fg);
    border:1px solid var(--line);background:var(--card);border-radius:9px;padding:8px 12px;min-width:0}
  #steps a.cur{border-color:var(--acc);box-shadow:inset 0 0 0 1px var(--acc)}
  #steps .n{flex:none;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;
    background:var(--line);color:var(--fg)}
  #steps .done .n{background:#0ca30c;color:#fff} #steps .todo .n{background:#fab219;color:#1d1d1b}
  #steps .t{display:flex;flex-direction:column;min-width:0} #steps b{font-size:13.5px}
  #steps small{color:var(--mut);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}`;
  document.head.insertAdjacentHTML('beforeend', `<style>${css}</style>`);
  const nav = document.getElementById('steps');
  if (!nav) return;
  const page = nav.dataset.page;
  const n = x => (x || 0).toLocaleString('en-US');
  const gb = b => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.round((b || 0) / 1e3) + ' KB';
  async function render() {
    let w;
    try { w = await (await fetch('/api/workflow')).json(); } catch (e) { return; }
    const s = w.scan || {}, c = w.catalog || {}, sel = w.selection || {}, t = w.transfer || {}, best = w.newest || {};
    const find = !c.txg ? 'build the catalog' : c.update ? 'newer state found – update catalog'
      : (s.running ? `scan ${(s.pct || 0).toFixed(0)} % · ` : '') + `using ${best.date || 'TXG ' + n(c.txg)}`;
    const steps = [
      ['/', 'status', 1, 'Find a state', find, !c.txg || c.update ? 'todo' : 'done'],
      ['/select', 'select', 2, 'Select files', sel.files ? `${n(sel.files)} file${sel.files === 1 ? '' : 's'} · ${gb(sel.bytes)}`
        : 'nothing selected yet', sel.files ? 'done' : ''],
      ['/transfer', 'transfer', 3, 'Transfer', t.running ? `running · ${n(t.files)} files copied`
        : (t.jobs ? `last: ${t.last_label} · ${t.last_state}` : 'not started'), t.done ? 'done' : ''],
    ];
    nav.innerHTML = steps.map(([href, pg, num, title, sub, st]) =>
      `<a href="${href}" class="${st}${pg === page ? ' cur' : ''}" title="${sub}">
        <span class="n">${st === 'done' ? '✓' : num}</span><span class="t"><b>${num} · ${title}</b><small>${sub}</small></span></a>`).join('');
  }
  render();
  setInterval(render, 10000);
})();
