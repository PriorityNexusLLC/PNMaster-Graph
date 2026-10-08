// Buttons on a report page: print/save as PDF, download the Markdown, copy the text, and the
// independent review (second pass) status with links to the reviewed version and your review notes.
const mdUrl = location.pathname.replace(/\.html$/, '.md');
const fid = location.pathname.split('/').pop().replace(/\.html$/, '').replace(/-reviewed$/, '');
const reviewed = /-reviewed\.html$/.test(location.pathname);
const msg = t => { document.getElementById('msg').textContent = t; };
document.getElementById('print').onclick = () => window.print();
const a = document.getElementById('md');
a.href = mdUrl;
a.setAttribute('download', mdUrl.split('/').pop());
document.getElementById('copy').onclick = async () => {
  try {
    const text = await fetch(mdUrl).then(r => r.text());
    await navigator.clipboard.writeText(text);
    msg('Copied. Paste into a new Substack post.');
  } catch { msg('Copy did not work here; use the download link instead.'); }
};

const bar = document.querySelector('.bar');
const box = document.createElement('div');
box.className = 'review';
bar.appendChild(box);
const link = (href, text) => { const x = document.createElement('a'); x.href = href; x.textContent = text; return x; };
const tone = v => /^(BLOCKED|ON HOLD)/.test(v) ? 'bad' : /^NEEDS/.test(v) ? 'warn' : /^READY/.test(v) ? 'ok' : 'next';

async function poll() {
  let s;
  try { s = await fetch(`/api/report/${fid}/status`).then(r => r.json()); } catch { return; }
  box.textContent = '';
  if (s.state === 'running' || s.state === 'pending') {
    box.textContent = 'Independent review running: re-opening every source filing…';
    setTimeout(poll, 3000);
    return;
  }
  if (s.state === 'error') { box.textContent = `Review stopped: ${s.error}. Try "Re-check" again later.`; }
  if (s.state === 'done') {
    const v = document.createElement('b');
    v.className = tone(s.verdict);
    v.textContent = s.verdict;
    box.append('Review: ', v, ` · sources ${s.sources_ok}/${s.sources} confirmed · `);
    if (!reviewed) box.append(link(s.reviewed_url, 'Open reviewed version (v2)'), ' · ');
    box.append(link(s.notes_url, 'Review notes (for you)'));
    if (!reviewed) document.body.classList.add('is-draft');
  }
  const btn = document.createElement('button');
  btn.textContent = 'Re-check my edited copy';
  btn.title = 'After you edit your working copy in Obsidian (e.g. fill in the right of reply), run the review again';
  btn.onclick = async () => {
    btn.disabled = true;
    await fetch(`/api/report/${fid}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    setTimeout(poll, 1500);
  };
  box.append(' ', btn);
}
poll();
