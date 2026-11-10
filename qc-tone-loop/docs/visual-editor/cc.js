(function(){
  var DATA = JSON.parse(document.getElementById('ccdata').textContent);
  var PER_PAGE = 10;
  var NS = 'http://www.w3.org/2000/svg';

  function esc(s){ return String(s).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }

  // JUCE-style skew: proportion = ((v-lo)/(hi-lo))^skew
  function prop(p){
    var span = (p.hi - p.lo) || 1;
    var x = Math.min(1, Math.max(0, (p.d - p.lo) / span));
    return Math.pow(x, p.sk || 1);
  }
  function fmt(p){
    if (p.ms && p.d <= p.lo) return esc(p.ms);
    var v = p.d, u = (p.u || '').toLowerCase(), s;
    if (p.int || u === '%' || u === 'bpm' || ((u === 'hz' || u === 'ms') && Math.abs(v) >= 10)) s = String(Math.round(v));
    else if (v !== 0 && Math.abs(v) < 1) s = v.toFixed(2);
    else s = v.toFixed(1);
    return esc(s) + (p.u ? '<i>' + esc(p.u) + '</i>' : '');
  }
  function arc(r, a0, a1){
    // angles in degrees, 0 = 12 o'clock, clockwise
    function pt(a){ var t = (a - 90) * Math.PI / 180; return [50 + r * Math.cos(t), 50 + r * Math.sin(t)]; }
    var p0 = pt(a0), p1 = pt(a1), large = (a1 - a0) > 180 ? 1 : 0;
    return 'M' + p0[0].toFixed(2) + ' ' + p0[1].toFixed(2) + 'A' + r + ' ' + r + ' 0 ' + large + ' 1 ' + p1[0].toFixed(2) + ' ' + p1[1].toFixed(2);
  }
  function knob(p){
    var a = -135 + 270 * prop(p);
    var t = (a - 90) * Math.PI / 180;
    var x = 50 + 27 * Math.cos(t), y = 50 + 27 * Math.sin(t);
    return '<svg class="cc-knob" viewBox="0 0 100 100" aria-hidden="true">' +
      '<path d="' + arc(44, -135, 135) + '" fill="none" stroke="#050505" stroke-width="8" stroke-linecap="butt"/>' +
      (a > -134.5 ? '<path d="' + arc(44, -135, a) + '" fill="none" stroke="var(--c)" stroke-width="8"/>' : '') +
      '<circle cx="50" cy="50" r="34" fill="#151515" stroke="#050505" stroke-width="4"/>' +
      '<line x1="50" y1="50" x2="' + x.toFixed(2) + '" y2="' + y.toFixed(2) + '" stroke="#fff" stroke-width="4.5" stroke-linecap="round"/>' +
      '</svg>';
  }
  function control(p){
    if (p.t === 'k') return '<div class="cc-body"><span class="cc-val">' + fmt(p) + '</span>' + knob(p) + '</div>';
    var i = Math.round(p.d - p.lo);
    if (p.st && p.st.length > 2) return '<div class="cc-body"><span class="cc-sel">' + esc(p.st[Math.min(p.st.length - 1, Math.max(0, i))]) + '</span></div>';
    var labels = p.st || [p.off || 'OFF', p.on || 'ON'];
    return '<div class="cc-body"><span class="cc-tog">' + labels.map(function(l, k){
      return '<span class="' + (k === i ? 'on' : '') + '">' + esc(l) + '</span>';
    }).join('') + '</span></div>';
  }
  function cells(m, page){
    var list = m.p.slice(page * PER_PAGE, (page + 1) * PER_PAGE);
    var h = list.map(function(p){
      return '<div class="cc-cell"><span class="cc-name">' + esc(p.n) + '</span>' + control(p) + '</div>';
    }).join('');
    var cols = 5, pad = (cols - (list.length % cols)) % cols;
    for (var k = 0; k < pad; k++) h += '<div class="cc-cell blank"></div>';
    return h;
  }
  function build(id, row){
    var m = DATA[id];
    var pages = Math.ceil(m.p.length / PER_PAGE);
    var name = row.querySelector('.dev').textContent;
    var title = m.c === 'cap' ? name : m.n;
    var el = document.createElement('div');
    el.className = 'cc';
    el.dataset.c = m.c;
    var tabs = '';
    if (pages > 1) {
      tabs = '<div class="cc-pages">';
      for (var i = 0; i < pages; i++) tabs += '<button type="button" data-pg="' + i + '" aria-pressed="' + (i === 0) + '">' + (i + 1) + '</button>';
      tabs += '</div>';
    }
    el.innerHTML = '<div class="cc-head"><span class="cc-dots">&#8942;</span><span class="cc-title">' + esc(m.cat) + ' <b>' + esc(title) + '</b></span>' + tabs + '</div>' +
      (m.p.length ? '<div class="cc-grid">' + cells(m, 0) + '</div>' : '<div class="cc-none">表示できるパラメータはありません。</div>');
    el.addEventListener('click', function(e){
      var b = e.target.closest('[data-pg]');
      if (!b) return;
      e.preventDefault();
      el.querySelectorAll('[data-pg]').forEach(function(x){ x.setAttribute('aria-pressed', String(x === b)); });
      el.querySelector('.cc-grid').innerHTML = cells(m, +b.dataset.pg);
    });
    return el;
  }
  document.addEventListener('click', function(e){
    var btn = e.target.closest('.pbtn');
    if (!btn) return;
    e.preventDefault();
    var row = btn.closest('.row');
    var open = row.querySelector('.cc');
    if (open) { open.remove(); btn.setAttribute('aria-expanded', 'false'); return; }
    row.appendChild(build(btn.dataset.m, row));
    btn.setAttribute('aria-expanded', 'true');
  });
})();
