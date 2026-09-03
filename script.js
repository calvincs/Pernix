/* =====================================================
   Pernix — product page
   1  hero glyph wall        2  hero replay
   3  install copy           4  star count
   5  cron tile ticker       6  reveal-on-scroll
   7  topbar                 8  state machine
   9  codex tabs            10  pipeline readout
  11  mobile menu           12  api language tabs
   ===================================================== */
/* =====================================================
   Pernix product page — interactivity
   - Animated Lissajous sigil (canvas)
   - Scroll reveals
   - State machine readout
   - Codex tabs (SOUL / RULES / Skill)
   - Topbar scrolled state
   - Mobile section menu
   ===================================================== */

(() => {
  'use strict';

  /* -----------------------------------------
     1. Hero — the glyph wall
     A texture of identifier-ish glyphs scrolling row by row at
     its own speed, with the harness's real vocabulary surfacing
     out of the noise in gold and sinking back. 24fps ceiling,
     paused offscreen and on a hidden tab, thinner on small
     screens and low-core machines.
     ----------------------------------------- */

  const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

  const wallEl = document.getElementById('wall');
  if (wallEl && wallEl.getContext) {
    const wx = wallEl.getContext('2d', { alpha: true });
    const narrow = matchMedia('(max-width: 860px)').matches;
    const cores = navigator.hardwareConcurrency || 2;
    const LOW = narrow || cores <= 4;

    const FS = LOW ? 12 : 11;         // glyph size
    const CW = LOW ? 18 : 14;         // column advance
    const RH = LOW ? 25 : 20;         // row height
    const FILL = LOW ? 0.30 : 0.48;   // fraction of cells carrying a glyph
    const MAXDPR = LOW ? 1 : 1.5;
    const FRAME = 1000 / 24;
    const GLYPHS = 'abcdefghijklmnopqrstuvwxyz_./:=<>[]{}()0123456789';

    // Pernix's own vocabulary — states, events, tools, paths, a schedule.
    const WORDS = [
      'idle_ready', 'scouting', 'processing', 'compacting', 'finalizing', 'awaiting_user',
      'session.state_changed', 'scout.done',
      'remember', 'recall', 'deep_recall', 'search_web', 'browse_web',
      'spawn_worker', 'await_workers', 'schedule_job', 'job_start', 'repl',
      'view_image', 'mcp_add_server',
      'data/memories/', 'SOUL.md', 'RULES.md', 'data/agent/spaces/<slug>/',
      '0 7 * * 1-5',
    ];

    // pre-baked alpha ramp: 0 → 0.20, so the hot loop never builds a string
    const INK = [];
    for (let i = 0; i <= 40; i++) INK[i] = 'rgba(212,168,67,' + (i / 200).toFixed(3) + ')';

    const rnd = (a, b) => a + Math.random() * (b - a);
    const glyph = () => GLYPHS[(Math.random() * GLYPHS.length) | 0];

    let W = 0, H = 0, cols = 0, rows = 0, span = 0;
    let grid = [], off = [], spd = [], VF = [];
    let ex = 0, ey = 0, er = 0, ex2 = 0, ey2 = 0, er2 = 0;
    let word = null, nextWord = 0;

    const vfade = (u) => {
      const top = Math.min(1, u / 0.10);
      const bot = 1 - Math.pow(Math.max(0, (u - 0.52) / 0.48), 1.35);
      return Math.max(0.03, top * Math.max(0.03, bot));
    };

    const build = () => {
      const r = wallEl.getBoundingClientRect();
      W = Math.max(1, Math.round(r.width));
      H = Math.max(1, Math.round(r.height));
      const dpr = Math.min(window.devicePixelRatio || 1, MAXDPR);
      wallEl.width = Math.round(W * dpr);
      wallEl.height = Math.round(H * dpr);
      wx.setTransform(dpr, 0, 0, dpr, 0, 0);
      wx.font = FS + "px 'DM Mono', ui-monospace, monospace";
      wx.textBaseline = 'top';

      cols = Math.ceil(W / CW) + 6;
      rows = Math.ceil(H / RH) + 1;
      span = cols * CW;
      grid = []; off = []; spd = []; VF = [];
      for (let y = 0; y < rows; y++) {
        off[y] = Math.random() * span;
        spd[y] = rnd(0.10, 0.55);
        VF[y] = vfade(rows > 1 ? y / (rows - 1) : 0);
        const line = new Array(cols);
        for (let x = 0; x < cols; x++) {
          line[x] = Math.random() < FILL
            ? { c: glyph(), a: rnd(0.05, 0.18), t: rnd(0.05, 0.18) }
            : null;
        }
        grid[y] = line;
      }
      // Erase disc — keeps the wall off the headline. Left column on a
      // two-column fold, top of the stack once the columns collapse.
      if (narrow || W < 980) {
        ex = W * 0.5; ey = H * 0.24; er = Math.max(W * 0.66, 260);
        er2 = 0;
      } else {
        ex = W * 0.27; ey = H * 0.5; er = Math.min(W * 0.44, H * 0.80);
        ex2 = W * 0.72; ey2 = H * 0.5; er2 = Math.min(W * 0.30, H * 0.62);
      }
      word = null;
      nextWord = 0;
    };

    const startWord = (now) => {
      const text = WORDS[(Math.random() * WORDS.length) | 0];
      if (cols < text.length + 6 || rows < 6) return;
      // Bias away from the middle band, where the erase disc eats it.
      const band = Math.random() < 0.4
        ? [1, Math.max(2, Math.floor(rows * 0.18))]
        : [Math.floor(rows * 0.62), rows - 2];
      const row = Math.min(rows - 2, Math.max(1,
        band[0] + ((Math.random() * Math.max(1, band[1] - band[0])) | 0)));
      // columns whose x lands fully on screen without crossing the wrap seam
      const lo = Math.ceil(off[row] / CW);
      const hi = Math.floor((off[row] + W - (text.length + 1) * CW) / CW);
      if (hi <= lo) return;
      word = {
        text,
        row,
        col: lo + ((Math.random() * (hi - lo)) | 0),
        born: now,
        al: new Array(text.length).fill(0),
        out: null,
      };
    };

    const stepWord = (now) => {
      if (!word) {
        if (now >= nextWord) startWord(now);
        return;
      }
      const w = word;
      const n = w.text.length;
      const inDone = (n - 1) * 70 + 240;
      const t = now - w.born;
      if (!w.out) {
        for (let i = 0; i < n; i++) {
          w.al[i] = Math.max(0, Math.min(1, (t - i * 70) / 240));
        }
        if (t > inDone + 1800) {
          w.out = [];
          for (let i = 0; i < n; i++) w.out.push(i);
          for (let i = n - 1; i > 0; i--) {
            const j = (Math.random() * (i + 1)) | 0;
            const tmp = w.out[i]; w.out[i] = w.out[j]; w.out[j] = tmp;
          }
          w.outAt = now;
        }
      } else {
        const ft = now - w.outAt;
        let live = false;
        for (let k = 0; k < n; k++) {
          const i = w.out[k];
          const p = Math.max(0, Math.min(1, (ft - k * 60) / 200));
          w.al[i] = 1 - p;
          if (w.al[i] > 0.01) live = true;
        }
        if (!live) {
          word = null;
          nextWord = now + (LOW ? rnd(4600, 8600) : rnd(2600, 5400));
        }
      }
    };

    const draw = () => {
      wx.clearRect(0, 0, W, H);
      for (let y = 0; y < rows; y++) {
        const vf = VF[y];
        if (vf < 0.03) continue;
        const py = y * RH;
        const ro = off[y];
        const line = grid[y];
        for (let x = 0; x < cols; x++) {
          const c = line[x];
          if (!c) continue;
          c.a += (c.t - c.a) * 0.06;
          let px = x * CW - ro;
          if (px < -CW) px += span;
          if (px > W || px < -CW) continue;
          let k = (c.a * vf * 200) | 0;
          if (k < 3) continue;
          if (k > 40) k = 40;
          wx.fillStyle = INK[k];
          wx.fillText(c.c, px, py);
        }
      }

      if (word) {
        const w = word;
        const py = w.row * RH;
        const ro = off[w.row];
        const vf = Math.max(0.45, VF[w.row]);
        if (!LOW) { wx.shadowColor = 'rgba(240,200,99,0.6)'; wx.shadowBlur = 7; }
        for (let i = 0; i < w.text.length; i++) {
          const a = w.al[i] * vf;
          if (a < 0.03) continue;
          let px = (w.col + i) * CW - ro;
          if (px < -CW) px += span;
          if (px > W || px < -CW) continue;
          wx.fillStyle = 'rgba(240,200,99,' + a.toFixed(3) + ')';
          wx.fillText(w.text[i], px, py);
        }
        wx.shadowBlur = 0;
      }

      // punch the wall down where the copy sits
      wx.globalCompositeOperation = 'destination-out';
      const g = wx.createRadialGradient(ex, ey, 0, ex, ey, er);
      g.addColorStop(0, 'rgba(0,0,0,0.95)');
      g.addColorStop(0.5, 'rgba(0,0,0,0.62)');
      g.addColorStop(1, 'rgba(0,0,0,0)');
      wx.fillStyle = g;
      wx.fillRect(0, 0, W, H);
      if (er2) {
        const g2 = wx.createRadialGradient(ex2, ey2, 0, ex2, ey2, er2);
        g2.addColorStop(0, 'rgba(0,0,0,0.8)');
        g2.addColorStop(0.55, 'rgba(0,0,0,0.42)');
        g2.addColorStop(1, 'rgba(0,0,0,0)');
        wx.fillStyle = g2;
        wx.fillRect(0, 0, W, H);
      }
      wx.globalCompositeOperation = 'source-over';
    };

    let raf = null, last = 0, onScreen = true;

    const tick = (now) => {
      raf = requestAnimationFrame(tick);
      if (!onScreen || document.hidden) { last = now; return; }
      if (now - last < FRAME) return;
      last = now;
      for (let y = 0; y < rows; y++) {
        off[y] += spd[y];
        if (off[y] >= span) off[y] -= span;
      }
      const samples = Math.max(6, (cols * rows * 0.012) | 0);
      for (let k = 0; k < samples; k++) {
        const y = (Math.random() * rows) | 0;
        const x = (Math.random() * cols) | 0;
        const c = grid[y][x];
        if (!c) continue;
        if (Math.random() < 0.55) c.t = rnd(0.04, 0.19);
        else c.c = glyph();
      }
      stepWord(now);
      draw();
    };

    const playWall = () => { if (raf === null) { last = 0; raf = requestAnimationFrame(tick); } };
    const stopWall = () => { if (raf !== null) { cancelAnimationFrame(raf); raf = null; } };

    build();

    if (REDUCED) {
      // one still frame, one word already formed
      startWord(0);
      if (word) word.al.fill(1);
      draw();
    } else {
      const heroEl = document.querySelector('.hero');
      if (heroEl && 'IntersectionObserver' in window) {
        new IntersectionObserver(([e]) => {
          onScreen = e.isIntersecting;
          if (onScreen) playWall(); else stopWall();
        }, { threshold: 0 }).observe(heroEl);
      }
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) stopWall(); else if (onScreen) playWall();
      });
      playWall();
    }

    let rz;
    window.addEventListener('resize', () => {
      clearTimeout(rz);
      rz = setTimeout(() => { build(); if (REDUCED) { startWord(0); if (word) word.al.fill(1); draw(); } }, 220);
    }, { passive: true });
  }

  /* -----------------------------------------
     2. Hero — the replay
     A scripted walk through three real turns: the five phases on
     the rail, the real state names in the badge, real tool names
     in the transcript. Pauses on hover, on focus, off-screen and
     on a hidden tab; reduced motion gets the finished transcript.
     ----------------------------------------- */

  const swBody = document.getElementById('sw-body');
  if (swBody) {
    const swTitle = document.getElementById('sw-title');
    const swSeq = document.getElementById('sw-seq');
    const swSr = document.getElementById('sw-sr');
    const swBadge = document.getElementById('sw-badge');
    const swDot = document.getElementById('sw-dot');
    const swCtx = document.getElementById('sw-ctx');
    const swWin = document.getElementById('replay');
    const railItems = Array.from(document.querySelectorAll('#rail li'));
    const btnToggle = document.getElementById('rp-toggle');
    const btnNext = document.getElementById('rp-next');
    const btnWatch = document.getElementById('watch-turn');

    const SCENARIOS = [
      {
        title: 'Morning brief',
        say: 'Replay 1 of 3, a morning brief. The scout picks three tools; the agent fetches the forecast and the headlines, schedules a weekday 7am job, answers, and reflect passes.',
        steps: [
          ['phase', 0], ['state', 'idle_ready'], ['ctx', '12%'],
          ['user', 'build my morning brief before I wake'],
          ['wait', 380],
          ['phase', 1], ['state', 'scouting'], ['wait', 820],
          ['scout', 'scout — 3 tools, 840ms', [
            ['approach', 'forecast, then headlines, then a standing job'],
            ['tools', 'http_get · search_web · schedule_job'],
            ['memory', 'prefers bullets · Austin, TX · nothing flagged'],
          ]],
          ['wait', 460],
          ['phase', 2], ['state', 'processing'], ['ctx', '19%'],
          ['tool', 'http_get', 'api.open-meteo.com', '✓ 0.4s', 880],
          ['tool', 'search_web', '"austin headlines"', '✓ 1.1s', 1240],
          ['tool', 'schedule_job', 'morning-brief · 0 7 * * 1-5', '✓ 0.2s', 820],
          ['ctx', '31%'],
          ['ans', 'Austin: overcast, high 100°F, precip 22%. Nothing flagged since yesterday.'],
          ['ans', 'Three headlines below. The weekday 7:00 job is live — I dry-ran it once and it produced a correct brief.'],
          ['wait', 380],
          ['phase', 3], ['state', 'finalizing'],
          ['meta', 'reflect.done · <b>pass</b>'],
          ['wait', 500],
          ['phase', 4],
          ['meta', 'session.title → “Morning brief” · 2 entries saved'],
          ['wait', 620], ['state', 'idle_ready'], ['phase', 5],
          ['wait', 2200],
        ],
      },
      {
        title: 'Auth branch',
        say: 'Replay 2 of 3, a question about last week. The agent recalls a memory entry and searches past sessions, then answers and cites the file it came from with its age.',
        steps: [
          ['phase', 0], ['state', 'idle_ready'], ['ctx', '9%'],
          ['user', 'what did we decide about the auth branch last week?'],
          ['wait', 380],
          ['phase', 1], ['state', 'scouting'], ['wait', 760],
          ['scout', 'scout — 2 tools, 610ms', [
            ['approach', 'memory first, then the session archive'],
            ['tools', 'recall · search_sessions'],
            ['memory', 'auth branch · refresh tokens · mobile client'],
          ]],
          ['wait', 440],
          ['phase', 2], ['state', 'processing'], ['ctx', '17%'],
          ['tool', 'recall', '"auth branch decision"', '✓ 0.3s', 820],
          ['tool', 'search_sessions', '"auth branch"', '✓ 0.6s', 980],
          ['ans', 'You dropped refresh-token rotation for now — the mobile client could not re-auth in the background, and the workaround was worse than the risk.'],
          ['cite', 'pernix.decisions.md', '6d ago'],
          ['wait', 460],
          ['phase', 3], ['state', 'finalizing'],
          ['meta', 'reflect.done · <b>pass</b>'],
          ['wait', 480],
          ['phase', 4],
          ['meta', 'session.title → “Auth branch” · 1 entry saved'],
          ['wait', 620], ['state', 'idle_ready'], ['phase', 5],
          ['wait', 2200],
        ],
      },
      {
        title: 'context7',
        say: 'Replay 3 of 3, wiring up an MCP server. Adding a server is a dangerous tool, so the session waits for approval, then queries the new server through its generated tool names.',
        steps: [
          ['phase', 0], ['state', 'idle_ready'], ['ctx', '11%'],
          ['user', 'wire up the context7 MCP server and check the FastAPI lifespan docs'],
          ['wait', 380],
          ['phase', 1], ['state', 'scouting'], ['wait', 780],
          ['scout', 'scout — 3 tools, 720ms', [
            ['approach', 'register the server, then read its docs tools'],
            ['tools', 'mcp_add_server · mcp_context7_*'],
            ['memory', 'remote MCP only on this box · never stdio'],
          ]],
          ['wait', 420],
          ['phase', 2], ['state', 'processing'], ['ctx', '21%'],
          ['ask', 'mcp_add_server is <b>dangerous</b> — add remote server “context7”?', 1600],
          ['tool', 'mcp_add_server', 'context7 · remote', '✓ 0.9s', 900],
          ['tool', 'mcp_context7_resolve_library_id', '"fastapi"', '✓ 0.3s', 760],
          ['tool', 'mcp_context7_get_library_docs', '"lifespan"', '✓ 0.8s', 1000],
          ['ans', 'context7 is registered — its tools are now mcp_context7_*, under the same scout curation and the same gate as the built-ins.'],
          ['ans', 'Lifespan replaces @app.on_event: one async context manager, startup above the yield, shutdown below.'],
          ['wait', 360],
          ['phase', 3], ['state', 'finalizing'],
          ['meta', 'reflect.done · <b>pass</b>'],
          ['wait', 480],
          ['phase', 4],
          ['meta', 'session.title → “context7” · 1 entry saved'],
          ['wait', 620], ['state', 'idle_ready'], ['phase', 5],
          ['wait', 2200],
        ],
      },
    ];

    const ABORT = { abort: true };
    let token = 0;
    let cur = 0;
    let userPaused = false;
    let hoverPaused = false;
    let offPaused = false;

    const held = () => userPaused || hoverPaused || offPaused || document.hidden;

    const sleep = (ms, tk) => new Promise((resolve, reject) => {
      let left = ms;
      let mark = performance.now();
      const step = () => {
        if (tk !== token) return reject(ABORT);
        const now = performance.now();
        if (!held()) left -= (now - mark);
        mark = now;
        if (left <= 0) return resolve();
        setTimeout(step, Math.min(left, 45));
      };
      setTimeout(step, Math.min(ms, 45));
    });

    const esc = (s) => String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

    const bottom = () => { swBody.scrollTop = swBody.scrollHeight; };

    const addRow = (cls, html) => {
      const d = document.createElement('div');
      d.className = 'r ' + cls;
      if (html != null) d.innerHTML = html;
      swBody.appendChild(d);
      bottom();
      return d;
    };

    const setPhase = (p) => {
      railItems.forEach((li, i) => {
        li.classList.toggle('on', i === p);
        li.classList.toggle('done', p >= 0 && i < p);
      });
    };

    const RUNNING = ['scouting', 'processing', 'compacting', 'finalizing'];
    const setState = (s) => {
      swBadge.textContent = s;
      swBadge.classList.toggle('run', RUNNING.indexOf(s) !== -1);
      swBadge.classList.toggle('wait', s === 'awaiting_user');
      swDot.classList.toggle('busy', s !== 'idle_ready');
    };

    const scoutHTML = (head, pairs) => {
      let h = '<div class="sc-h">' + esc(head) + '</div>';
      pairs.forEach((kv) => {
        h += '<div class="sc-row"><span class="sc-k">' + esc(kv[0]) +
             '</span><span class="sc-v">' + esc(kv[1]) + '</span></div>';
      });
      return h;
    };

    const citeHTML = (file, age) => '<span>' + esc(file) + '</span><i>' + esc(age) + '</i>';

    async function typeInto(el, text, tk) {
      for (let i = 1; i <= text.length; i++) {
        if (tk !== token) throw ABORT;
        el.innerHTML = esc(text.slice(0, i)) + '<span class="cur"></span>';
        bottom();
        await sleep(32, tk);
      }
      el.textContent = text;
      bottom();
    }

    async function streamInto(el, text, tk) {
      const parts = text.split(' ');
      let acc = '';
      for (let i = 0; i < parts.length; i++) {
        if (tk !== token) throw ABORT;
        acc += (i ? ' ' : '') + parts[i];
        el.textContent = acc;
        bottom();
        await sleep(52, tk);
      }
    }

    async function exec(st, tk) {
      switch (st[0]) {
        case 'phase': setPhase(st[1]); return;
        case 'state': setState(st[1]); return;
        case 'ctx': swCtx.textContent = st[1]; return;
        case 'wait': return sleep(st[1], tk);
        case 'user': return typeInto(addRow('r-user', ''), st[1], tk);
        case 'scout': addRow('r-scout', scoutHTML(st[1], st[2])); return sleep(240, tk);
        case 'ans': return streamInto(addRow('r-ans', ''), st[1], tk);
        case 'cite': addRow('cite', citeHTML(st[1], st[2])); return sleep(280, tk);
        case 'meta': addRow('r-meta', st[1]); return sleep(140, tk);
        case 'tool': {
          const d = addRow('r-tool',
            '<span class="t-name">' + esc(st[1]) + '</span>' +
            '<span class="t-arg">' + esc(st[2]) + '</span>' +
            '<span class="t-res pending">· · ·</span>');
          await sleep(st[4], tk);
          if (tk !== token) throw ABORT;
          const res = d.querySelector('.t-res');
          res.className = 't-res';
          res.textContent = st[3];
          return;
        }
        case 'ask': {
          const d = addRow('r-ask',
            '<span>' + st[1] + '</span><span class="ask-a">waiting…</span>');
          setState('awaiting_user');
          await sleep(st[2], tk);
          if (tk !== token) throw ABORT;
          d.querySelector('.ask-a').textContent = 'approved';
          setState('processing');
          return;
        }
      }
    }

    function reset(idx) {
      cur = idx;
      const sc = SCENARIOS[idx];
      swBody.innerHTML = '';
      swTitle.textContent = sc.title;
      swSeq.textContent = (idx + 1) + ' / ' + SCENARIOS.length;
      if (swSr) swSr.textContent = sc.say;
      setPhase(-1);
      setState('idle_ready');
      return sc;
    }

    // Reduced motion: the finished transcript, no timers.
    function renderStill(idx) {
      const sc = reset(idx);
      let lastPhase = -1;
      sc.steps.forEach((st) => {
        switch (st[0]) {
          case 'phase': if (st[1] >= 0) lastPhase = st[1]; break;
          case 'ctx': swCtx.textContent = st[1]; break;
          case 'user': { const d = addRow('r-user', ''); d.textContent = st[1]; break; }
          case 'scout': addRow('r-scout', scoutHTML(st[1], st[2])); break;
          case 'ans': { const d = addRow('r-ans', ''); d.textContent = st[1]; break; }
          case 'cite': addRow('cite', citeHTML(st[1], st[2])); break;
          case 'meta': addRow('r-meta', st[1]); break;
          case 'tool':
            addRow('r-tool',
              '<span class="t-name">' + esc(st[1]) + '</span>' +
              '<span class="t-arg">' + esc(st[2]) + '</span>' +
              '<span class="t-res">' + esc(st[3]) + '</span>');
            break;
          case 'ask':
            addRow('r-ask', '<span>' + st[1] + '</span><span class="ask-a">approved</span>');
            break;
        }
      });
      setPhase(Math.min(lastPhase, railItems.length - 1));
      setState('idle_ready');
      bottom();
    }

    async function play(idx) {
      const tk = ++token;
      const sc = reset(idx);
      try {
        for (let i = 0; i < sc.steps.length; i++) await exec(sc.steps[i], tk);
      } catch (e) {
        if (e !== ABORT) throw e;
        return;
      }
      if (tk === token) play((idx + 1) % SCENARIOS.length);
    }

    const restart = (idx) => { if (REDUCED) renderStill(idx); else play(idx); };

    // The window's resting state is a finished turn — a thumbnail, a shared
    // link or a two-second skim all land on something already worth reading.
    async function opening() {
      const tk = ++token;
      renderStill(0);
      try {
        await sleep(2500, tk);
      } catch (e) {
        if (e !== ABORT) throw e;
        return;
      }
      if (tk === token) play(1);
    }

    if (REDUCED) {
      renderStill(0);
      if (btnToggle) btnToggle.hidden = true;
    } else {
      const stage = document.querySelector('.hero-stage');
      swWin.addEventListener('mouseenter', () => { hoverPaused = true; });
      swWin.addEventListener('mouseleave', () => { hoverPaused = false; });
      // Focus inside the transcript holds it still — but not focus on the
      // controls themselves, which would freeze the replay the moment you
      // clicked 'next'.
      if (stage) {
        stage.addEventListener('focusin', (e) => {
          if (!e.target.closest('.replay-ctl')) hoverPaused = true;
        });
        stage.addEventListener('focusout', () => { hoverPaused = false; });
      }
      if ('IntersectionObserver' in window) {
        new IntersectionObserver(([e]) => { offPaused = !e.isIntersecting; },
          { threshold: 0 }).observe(swWin);
      }
      btnToggle.addEventListener('click', () => {
        userPaused = !userPaused;
        btnToggle.textContent = userPaused ? 'play' : 'pause';
        btnToggle.setAttribute('aria-pressed', userPaused ? 'true' : 'false');
      });
      opening();
    }

    btnNext.addEventListener('click', () => restart((cur + 1) % SCENARIOS.length));

    if (btnWatch) {
      btnWatch.addEventListener('click', () => {
        swWin.scrollIntoView({ behavior: REDUCED ? 'auto' : 'smooth', block: 'center' });
        userPaused = false;
        if (btnToggle) { btnToggle.textContent = 'pause'; btnToggle.setAttribute('aria-pressed', 'false'); }
        restart(0);
      });
    }
  }

  /* -----------------------------------------
     3. Hero — copy the install strip
     ----------------------------------------- */

  const copyBtn = document.getElementById('copy-install');
  const installBody = document.getElementById('install-body');
  if (copyBtn && installBody) {
    const text = Array.from(installBody.querySelectorAll('.il'))
      .map((l) => l.textContent.replace(/^\s*\$\s*/, ''))
      .join('\n');

    const flash = (ok) => {
      copyBtn.textContent = ok ? 'copied' : 'select it';
      copyBtn.classList.toggle('done', ok);
      setTimeout(() => { copyBtn.textContent = 'copy'; copyBtn.classList.remove('done'); }, 1800);
    };

    copyBtn.addEventListener('click', async () => {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          flash(true);
          return;
        }
      } catch (e) { /* fall through to the textarea */ }
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        flash(ok);
      } catch (e) {
        flash(false);
      }
    });
  }

  /* -----------------------------------------
     4. Live star count
     Best-effort: the page reads fine without it, and it is never
     attempted from a file:// preview.
     ----------------------------------------- */

  const starCount = document.getElementById('star-count');
  const navStars = document.getElementById('nav-stars');
  if (starCount && /^https?:$/.test(location.protocol)) {
    const ctl = ('AbortController' in window) ? new AbortController() : null;
    const kill = setTimeout(() => { if (ctl) ctl.abort(); }, 4000);
    const opts = { headers: { Accept: 'application/vnd.github+json' } };
    if (ctl) opts.signal = ctl.signal;
    fetch('https://api.github.com/repos/calvincs/Pernix', opts)
      .then((r) => (r && r.ok ? r.json() : null))
      .then((d) => {
        if (!d || typeof d.stargazers_count !== 'number') return;
        const n = d.stargazers_count >= 1000
          ? (d.stargazers_count / 1000).toFixed(1).replace(/\.0$/, '') + 'k'
          : String(d.stargazers_count);
        starCount.textContent = '★ ' + n;
        if (navStars) navStars.textContent = '★' + n + ' ';
      })
      .catch(() => { /* offline, rate-limited or blocked — keep the static label */ })
      .then(() => clearTimeout(kill));
  }

  /* -----------------------------------------
     5. Cron tile — the weekday marker walks
     ----------------------------------------- */

  const weekEl = document.getElementById('week');
  if (weekEl && !REDUCED && 'IntersectionObserver' in window) {
    const days = Array.from(weekEl.children);
    const lit = [0, 1, 2, 3, 4];
    let k = 0, timer = null;
    const stepDay = () => {
      days.forEach((d) => d.classList.remove('now'));
      days[lit[k]].classList.add('now');
      k = (k + 1) % lit.length;
    };
    new IntersectionObserver(([e]) => {
      if (e.isIntersecting && !timer) { stepDay(); timer = setInterval(stepDay, 1150); }
      else if (!e.isIntersecting && timer) {
        clearInterval(timer); timer = null;
        days.forEach((d) => d.classList.remove('now'));
      }
    }, { threshold: 0.35 }).observe(weekEl);
  }

  /* -----------------------------------------
     6. Reveal-on-scroll
     ----------------------------------------- */

  const revealEls = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) {
          e.target.classList.add('in');
          io.unobserve(e.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
    revealEls.forEach((el) => io.observe(el));
  } else {
    revealEls.forEach((el) => el.classList.add('in'));
  }

  /* -----------------------------------------
     7. Topbar — scrolled state
     ----------------------------------------- */

  const topbar = document.querySelector('.topbar');
  if (topbar) {
    const onScroll = () => {
      if (window.scrollY > 30) topbar.classList.add('scrolled');
      else topbar.classList.remove('scrolled');
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  /* -----------------------------------------
     8. State machine readout
     ----------------------------------------- */

  const stateInfo = {
    idle:       { name: 'idle_ready',       desc: 'No active turn. The session is open and waiting for a prompt. The reaper will reap it from memory after 30 minutes of stillness with no subscribers.', meta: 'awaiting input' },
    scout:      { name: 'scouting',         desc: 'A small fast model in a fresh context plans the approach — searches memory, picks tools, decides which skills to load, drafts a ScoutReport.', meta: 'fast model · fresh context' },
    processing: { name: 'processing',       desc: 'The main agent loop runs. Streams tokens, executes tool calls, appends results, and loops until a final answer or a round ceiling.', meta: 'main model · streaming' },
    compacting: { name: 'compacting',       desc: 'Context exceeded the proactive or critical threshold. Old turns are summarized into a digest. Originals stay in the database — only the prompt view changes.', meta: 'view transform' },
    finalizing: { name: 'finalizing',       desc: 'Post-hooks run after the user has seen the answer: auto-titling, memory distillation, worker cleanup, reflection feedback into the next turn.', meta: 'background · ~seconds' },
    cancelling: { name: 'cancelling',       desc: 'You pressed cancel. The agent is being torn down at the next safe boundary. The reaper force-unsticks after 30s.', meta: 'user-initiated' },
    awaiting:   { name: 'awaiting_user',    desc: 'The agent called ask_user and is waiting for your reply. No LLM resources are being spent. As soon as you answer, a new turn begins.', meta: 'paused for input' },
    pause_req:  { name: 'pause_requested',  desc: 'A worker has been asked to pause. It will observe the request at the next round boundary and transition to PAUSED.', meta: 'in transit' },
    paused:     { name: 'paused',           desc: 'A worker is parked, waiting for a resume signal. Useful for "wait, let me think." The safety net cancels after 24h.', meta: 'frozen' },
    awaiting_workers: { name: 'awaiting_workers', desc: 'The main loop spawned one or more workers and is parked while they run. As soon as the watched workers finish — or are cancelled — the parent session resumes its turn.', meta: 'parent · waiting on children' },
  };

  const stateNameEl = document.getElementById('state-name');
  const stateDescEl = document.getElementById('state-desc');
  const stateMetaEl = document.getElementById('state-meta');
  const stateOutEl = document.getElementById('state-edges-out');
  const snodes = document.querySelectorAll('.snode');

  // ---- The real graph. Node positions in % of the diagram box; edges and
  // reason vocabulary mirror TRANSITIONS in sessions/state_v2.py (the two
  // housekeeping reasons, reaper-unstick and cancel-timeout, are omitted
  // from the drawing and noted in the section footer instead).
  const NPOS = {
    idle: [46, 8], scout: [76, 18], compacting: [92, 30], processing: [58, 46],
    awaiting_workers: [88, 66], awaiting: [66, 82], finalizing: [38, 86],
    paused: [22, 58], pause_req: [26, 28], cancelling: [44, 26],
  };
  const SEDGES = [
    { f: 'idle',             t: 'scout',            cls: 'happy', bow: -0.06,  r: 'prompt-arrived' },
    { f: 'scout',            t: 'processing',       cls: 'happy', bow: 0.05,  r: 'scout-done' },
    { f: 'processing',       t: 'finalizing',       cls: 'happy', bow: 0.05,  r: 'loop-complete' },
    { f: 'finalizing',       t: 'idle',             cls: 'happy', bow: -1.4,  r: 'turn-complete' },
    { f: 'finalizing',       t: 'scout',            cls: 'retry', bow: 0.35, r: 'reflect-retry' },
    { f: 'processing',       t: 'compacting',       cls: 'flow',  bow: 0.10,  r: 'compact-proactive' },
    { f: 'compacting',       t: 'processing',       cls: 'flow',  bow: 0.10,  r: 'compact-done' },
    { f: 'processing',       t: 'awaiting',         cls: 'flow',  bow: 0.06,  r: 'ask-user' },
    { f: 'awaiting',         t: 'scout',            cls: 'flow',  bow: 0.22,  r: 'answer-received' },
    { f: 'processing',       t: 'awaiting_workers', cls: 'flow',  bow: 0.06,  r: 'workers-dispatched' },
    { f: 'awaiting_workers', t: 'scout',            cls: 'flow',  bow: -0.16, r: 'workers-complete' },
    { f: 'processing',       t: 'pause_req',        cls: 'flow',  bow: 0.06,  r: 'pause-requested' },
    { f: 'pause_req',        t: 'paused',           cls: 'flow',  bow: 0.08,  r: 'pause-observed' },
    { f: 'paused',           t: 'processing',       cls: 'flow',  bow: 0.08,  r: 'resume' },
    { f: 'processing',       t: 'cancelling',       cls: 'halt',  bow: 0.05,  r: 'cancel-requested' },
    { f: 'cancelling',       t: 'idle',             cls: 'halt',  bow: 0.06,  r: 'cancel-complete' },
  ];
  // Full legal-exit list per state (includes edges not drawn), for the readout.
  const OUTS = {
    idle:             [['prompt-arrived', 'scouting']],
    scout:            [['scout-done', 'processing'], ['scout-error', 'finalizing'], ['cancel-requested', 'cancelling']],
    processing:       [['loop-complete', 'finalizing'], ['compact-proactive / critical / overflow', 'compacting'], ['ask-user', 'awaiting_user'], ['workers-dispatched', 'awaiting_workers'], ['pause-requested', 'pause_requested'], ['round-ceiling / agent-error', 'finalizing'], ['cancel-requested', 'cancelling']],
    compacting:       [['compact-done', 'processing'], ['compaction-failed / agent-error', 'finalizing'], ['cancel-requested', 'cancelling']],
    pause_req:        [['pause-observed', 'paused'], ['cancel-during-pause', 'cancelling']],
    paused:           [['resume', 'processing'], ['cancel-during-pause', 'cancelling']],
    cancelling:       [['cancel-complete / cancel-timeout', 'idle_ready']],
    finalizing:       [['turn-complete / finalize-error', 'idle_ready'], ['reflect-retry / eval-retry', 'scouting']],
    awaiting:         [['answer-received', 'scouting'], ['question-dismissed', 'idle_ready'], ['cancel-requested', 'cancelling']],
    awaiting_workers: [['workers-complete', 'scouting'], ['worker-timeout', 'idle_ready'], ['cancel-requested', 'cancelling']],
  };

  // ---- Draw the edges as quadratic curves between node centres.
  const VBW = 760, VBH = 460, TRIM = 30;
  const edgesG = document.getElementById('state-edges');
  const pulseEl = document.getElementById('state-pulse');
  const edgePaths = {};

  const npx = (key) => [NPOS[key][0] / 100 * VBW, NPOS[key][1] / 100 * VBH];

  if (edgesG) {
    SEDGES.forEach((e) => {
      const [x1, y1] = npx(e.f);
      const [x2, y2] = npx(e.t);
      const dx = x2 - x1, dy = y2 - y1;
      const len = Math.hypot(dx, dy);
      const mx = (x1 + x2) / 2 - dy / len * (e.bow * len);
      const my = (y1 + y2) / 2 + dx / len * (e.bow * len);
      // Trim both ends along the tangent so arrowheads sit outside the chips
      const t1 = Math.hypot(mx - x1, my - y1);
      const t2 = Math.hypot(mx - x2, my - y2);
      const ax = x1 + (mx - x1) / t1 * TRIM, ay = y1 + (my - y1) / t1 * TRIM;
      const bx = x2 + (mx - x2) / t2 * TRIM, by = y2 + (my - y2) / t2 * TRIM;
      const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      p.setAttribute('d', `M ${ax},${ay} Q ${mx},${my} ${bx},${by}`);
      p.setAttribute('class', `sedge ${e.cls}`);
      p.dataset.from = e.f;
      p.dataset.to = e.t;
      edgesG.appendChild(p);
      edgePaths[`${e.f}>${e.t}`] = p;
    });
  }

  const clearLit = () => {
    Object.values(edgePaths).forEach((p) => p.classList.remove('lit', 'faded'));
  };

  function activateState(node, meta) {
    const key = node.dataset.state;
    const info = stateInfo[key];
    if (!info) return;
    snodes.forEach((n) => n.classList.remove('active'));
    node.classList.add('active');
    if (stateNameEl) stateNameEl.textContent = info.name;
    if (stateDescEl) stateDescEl.textContent = info.desc;
    if (stateMetaEl) stateMetaEl.textContent = meta || info.meta;
    // Light this state's real outgoing edges, fade the rest
    Object.values(edgePaths).forEach((p) => {
      const mine = p.dataset.from === key;
      p.classList.toggle('lit', mine);
      p.classList.toggle('faded', !mine);
    });
    // Legal exits in the readout
    if (stateOutEl) {
      stateOutEl.innerHTML = '';
      (OUTS[key] || []).forEach(([reason, dest]) => {
        const li = document.createElement('li');
        const r = document.createElement('span');
        r.className = 'rsn';
        r.textContent = reason;
        const d = document.createElement('span');
        d.textContent = '→ ' + dest;
        li.appendChild(r);
        li.appendChild(d);
        stateOutEl.appendChild(li);
      });
    }
  }

  snodes.forEach((node) => {
    node.addEventListener('mouseenter', () => { stopTrace(); activateState(node); });
    node.addEventListener('focus', () => { stopTrace(); activateState(node); });
    node.addEventListener('click', () => { stopTrace(); activateState(node); });
  });

  // ---- Trace: a pulse walks an actual turn through the table —
  // compaction round-trip, a worker fan-out, and a reflect retry included.
  const TRACE = [
    'idle>scout', 'scout>processing', 'processing>compacting', 'compacting>processing',
    'processing>awaiting_workers', 'awaiting_workers>scout', 'scout>processing',
    'processing>finalizing', 'finalizing>scout', 'scout>processing',
    'processing>finalizing', 'finalizing>idle',
  ];
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const EDGE_MS = 1300, DWELL_MS = 650;
  let tracing = false;
  let traceRaf = null;
  let traceTimeout = null;

  const stopTrace = () => {
    tracing = false;
    if (traceRaf) { cancelAnimationFrame(traceRaf); traceRaf = null; }
    if (traceTimeout) { clearTimeout(traceTimeout); traceTimeout = null; }
    if (pulseEl) pulseEl.setAttribute('opacity', '0');
    clearLit();
  };

  const startTrace = () => {
    if (tracing || !pulseEl) return;
    // On narrow layouts the SVG is display:none (nodes become a chip grid);
    // path measurement is meaningless there, so skip the walking pulse.
    const svgEl = document.querySelector('.state-svg');
    if (!svgEl || svgEl.getClientRects().length === 0) return;
    tracing = true;
    let idx = 0;

    const step = () => {
      if (!tracing) return;
      const key = TRACE[idx % TRACE.length];
      const path = edgePaths[key];
      const edge = SEDGES.find((e) => `${e.f}>${e.t}` === key);
      if (!path || !edge) { tracing = false; return; }

      if (reducedMotion) {
        // No pulse for reduced-motion users — just step the highlight
        const node = document.querySelector(`.snode[data-state="${edge.t}"]`);
        if (node) activateState(node, `reason: ${edge.r}`);
        idx += 1;
        traceTimeout = setTimeout(step, 2800);
        return;
      }

      clearLit();
      path.classList.add('lit');
      const total = path.getTotalLength();
      const start = performance.now();
      pulseEl.setAttribute('opacity', '1');

      const frame = (now) => {
        if (!tracing) return;
        const u = Math.min((now - start) / EDGE_MS, 1);
        const eased = u * u * (3 - 2 * u);
        const pt = path.getPointAtLength(eased * total);
        pulseEl.setAttribute('cx', pt.x);
        pulseEl.setAttribute('cy', pt.y);
        if (u < 1) {
          traceRaf = requestAnimationFrame(frame);
        } else {
          const node = document.querySelector(`.snode[data-state="${edge.t}"]`);
          if (node) activateState(node, `reason: ${edge.r}`);
          path.classList.add('lit'); // keep the travelled edge lit through the dwell
          idx += 1;
          traceTimeout = setTimeout(step, DWELL_MS);
        }
      };
      traceRaf = requestAnimationFrame(frame);
    };
    step();
  };

  // Trace only while the section is visible; pause while the user explores.
  const statesSection = document.getElementById('states');
  if (statesSection && 'IntersectionObserver' in window) {
    const io2 = new IntersectionObserver((entries) => {
      entries.forEach((e) => e.isIntersecting ? startTrace() : stopTrace());
    }, { threshold: 0.4 });
    io2.observe(statesSection);
  }
  const stateShell = document.querySelector('.state-shell');
  stateShell?.addEventListener('mouseleave', () => { if (!tracing) startTrace(); });

  /* -----------------------------------------
     9. Codex tabs — SOUL / RULES / Skill
     ----------------------------------------- */

  const tabs = document.querySelectorAll('.codex-tab');
  const panes = document.querySelectorAll('.codex-body');

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      tabs.forEach((t) => t.classList.toggle('active', t === tab));
      panes.forEach((p) => p.classList.toggle('hidden', p.dataset.pane !== target));
    });
  });

  /* -----------------------------------------
     10. Pipeline event-log readout
     ----------------------------------------- */

  const pipeLog = document.getElementById('pipe-log');
  const pipeReadout = document.querySelector('.pipe-readout');
  const phaseEls = document.querySelectorAll('.phase[data-log]');
  const PIPE_DEFAULT = pipeLog ? pipeLog.textContent : '';

  phaseEls.forEach((ph) => {
    const show = () => {
      phaseEls.forEach((p) => p.classList.toggle('active', p === ph));
      if (pipeLog) pipeLog.textContent = ph.dataset.log;
      pipeReadout?.classList.add('lit');
    };
    const hide = () => {
      ph.classList.remove('active');
      if (pipeLog) pipeLog.textContent = PIPE_DEFAULT;
      pipeReadout?.classList.remove('lit');
    };
    ph.addEventListener('mouseenter', show);
    ph.addEventListener('focus', show);
    ph.addEventListener('mouseleave', hide);
    ph.addEventListener('blur', hide);
  });

  /* -----------------------------------------
     11. Mobile section menu
     Below 980px the primary nav collapses into a panel the
     button opens. Escape closes it and hands focus back;
     picking a section or tapping away closes it too.
     ----------------------------------------- */

  const navToggle = document.querySelector('.nav-toggle');
  const topnav = document.getElementById('topnav');

  if (navToggle && topnav) {
    const isOpen = () => navToggle.getAttribute('aria-expanded') === 'true';
    const setOpen = (open) => {
      navToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      topnav.classList.toggle('open', open);
    };
    const close = (refocus) => {
      if (!isOpen()) return;
      setOpen(false);
      if (refocus) navToggle.focus();
    };

    navToggle.addEventListener('click', () => setOpen(!isOpen()));
    topnav.addEventListener('click', (e) => {
      if (e.target.closest('a')) close(false);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' || e.key === 'Esc') close(true);
    });
    document.addEventListener('click', (e) => {
      if (!topnav.contains(e.target) && !navToggle.contains(e.target)) close(false);
    });

    // Back on a desktop viewport the panel is a plain row again.
    const wide = matchMedia('(min-width: 981px)');
    const onWide = () => { if (wide.matches) setOpen(false); };
    if (wide.addEventListener) wide.addEventListener('change', onWide);
    else if (wide.addListener) wide.addListener(onWide);
  }

  /* -----------------------------------------
     12. API code-sample language tabs
     ----------------------------------------- */

  const langTabs = document.querySelectorAll('.lang-tab');
  const langPanes = document.querySelectorAll('[data-lang-pane]');

  langTabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.lang;
      langTabs.forEach((t) => t.classList.toggle('active', t === tab));
      langPanes.forEach((p) => p.classList.toggle('hidden', p.dataset.langPane !== target));
    });
  });

})();
