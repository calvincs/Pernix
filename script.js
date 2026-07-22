/* =====================================================
   Pernix product page — interactivity
   - Animated Lissajous sigil (canvas)
   - Scroll reveals
   - State machine readout
   - Codex tabs (SOUL / RULES / Skill)
   - Topbar scrolled state
   ===================================================== */

(() => {
  'use strict';

  /* -----------------------------------------
     1. The sigil — animated string-art diamond
     Echoes the Pernix app icon: nested diamonds, each
     filled with parabolic envelopes formed by N straight
     lines connecting points on adjacent edges. Slow
     multi-rate rotation gives it the breathing-flow look.
     ----------------------------------------- */

  const canvas = document.getElementById('sigil');
  if (canvas) {
    const ctx = canvas.getContext('2d', { alpha: true });

    // ---- Performance tiering (mobile / low-power → fewer strings, lower DPR, no glow)
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const small = matchMedia('(max-width: 768px)').matches;
    const cores = navigator.hardwareConcurrency || 2;
    const lowPower = small || cores <= 4 ||
      /Android|webOS|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);

    const TIER = lowPower ? 'low' : 'high';
    const MAX_DPR = TIER === 'low' ? 1.5 : 2;
    const TARGET_MS = TIER === 'low' ? 33 : 16; // 30fps mobile, 60fps desktop

    let w, h, cx, cy, dpr;

    const resize = () => {
      dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
      const rect = canvas.getBoundingClientRect();
      w = rect.width;
      h = rect.height;
      canvas.width = Math.floor(w * dpr);
      canvas.height = Math.floor(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cx = w / 2;
      cy = h / 2;
    };
    resize();
    let resizeT;
    window.addEventListener('resize', () => {
      clearTimeout(resizeT);
      resizeT = setTimeout(resize, 120);
    }, { passive: true });

    // ---- Filament bundles — all anchored to the SAME 4 outer corners.
    // No nested diamonds: every filament connects two adjacent OUTER corners.
    // The "depth" feel comes from multiple bundles with overlapping bow ranges
    // (shallow to deep), all sharing the same anchor points. The bundles blend
    // into one another rather than reading as concentric rings.
    //
    // Each bundle: bow sweeps from `bowFrom` (j=0, hugs the edge) to a depth
    // that breathes between `baseDepth` (exhaled, shallow) and `peakDepth`
    // (inhaled, dramatically inward). Ranges deliberately overlap.
    // Each bundle carries its own colour — the deepest runs warmer (ember)
    // so the veil reads as having depth instead of one flat gold.
    const layers = TIER === 'low' ? [
      { N: 14, alpha: 0.34, lw: 0.45, bowFrom: -0.005, baseDepth: -0.08, peakDepth: -0.18, col: '212,168,67' },
      { N: 16, alpha: 0.40, lw: 0.45, bowFrom: -0.04,  baseDepth: -0.16, peakDepth: -0.28, col: '212,168,67' },
      { N: 12, alpha: 0.30, lw: 0.40, bowFrom: -0.10,  baseDepth: -0.22, peakDepth: -0.36, col: '198,128,46' },
    ] : [
      { N: 22, alpha: 0.36, lw: 0.45, bowFrom: -0.005, baseDepth: -0.08, peakDepth: -0.18, col: '212,168,67' },
      { N: 26, alpha: 0.42, lw: 0.45, bowFrom: -0.04,  baseDepth: -0.16, peakDepth: -0.30, col: '212,168,67' },
      { N: 22, alpha: 0.32, lw: 0.40, bowFrom: -0.10,  baseDepth: -0.22, peakDepth: -0.38, col: '198,128,46' },
    ];

    // ---- Pointer parallax (fine pointers only) — the veil leans gently
    // toward the cursor with inertia. Touch devices and reduced-motion skip it.
    const finePointer = matchMedia('(pointer: fine)').matches;
    const parTarget = { x: 0, y: 0 };
    const par = { x: 0, y: 0 };
    if (finePointer && !reduced) {
      window.addEventListener('pointermove', (e) => {
        parTarget.x = (e.clientX / window.innerWidth - 0.5) * 2;
        parTarget.y = (e.clientY / window.innerHeight - 0.5) * 2;
      }, { passive: true });
    }

    // Cardinal corner offsets for a diamond (square rotated 45°)
    const CORNER_ANGLES = [0, Math.PI / 2, Math.PI, 3 * Math.PI / 2];

    // Draw one filament bundle, all curves connecting the 4 outer corners.
    // Bow sweeps from `bowShallow` (j=0, hugs the edge) → `bowDeep` (j=N-1,
    // arches toward the diamond center). On top: a multi-frequency wobble and
    // a tiny along-edge offset, both amplified by `breath` for whimsical motion.
    const drawDiamond = (R, a, N, alpha, lw, bowShallow, bowDeep, t, breath, col = '212,168,67') => {
      const cs = [
        [cx + Math.cos(a + CORNER_ANGLES[0]) * R, cy + Math.sin(a + CORNER_ANGLES[0]) * R],
        [cx + Math.cos(a + CORNER_ANGLES[1]) * R, cy + Math.sin(a + CORNER_ANGLES[1]) * R],
        [cx + Math.cos(a + CORNER_ANGLES[2]) * R, cy + Math.sin(a + CORNER_ANGLES[2]) * R],
        [cx + Math.cos(a + CORNER_ANGLES[3]) * R, cy + Math.sin(a + CORNER_ANGLES[3]) * R],
      ];

      // Two phase clocks running at different rates → quasi-random shimmer
      const ph1 = t * 0.00065;
      const ph2 = t * 0.00112;
      const chaos = 0.4 + breath * 0.8; // 0.4 (calm) → 1.2 (whimsical at peak)

      ctx.beginPath();
      for (let i = 0; i < 4; i++) {
        const A = cs[i];
        const B = cs[(i + 1) & 3];
        const mx = (A[0] + B[0]) * 0.5;
        const my = (A[1] + B[1]) * 0.5;
        const ex = B[0] - A[0];
        const ey = B[1] - A[1];
        const len = Math.sqrt(ex * ex + ey * ey);
        const exn = ex / len, eyn = ey / len;   // along-edge unit
        const px = -eyn,    py = exn;            // perpendicular (right-hand)
        const sign = ((mx - cx) * px + (my - cy) * py) > 0 ? 1 : -1;

        const invN = N === 1 ? 0 : 1 / (N - 1);
        for (let j = 0; j < N; j++) {
          const u = N === 1 ? 0.5 : j * invN;
          // Two-harmonic perpendicular wobble — the second harmonic only kicks
          // in with breath, adding whimsy that blooms at peak inhale.
          const w1 = Math.sin(ph1 + j * 0.71 + i * 1.37) * 0.012 * chaos;
          const w2 = Math.sin(ph2 * 1.7 + j * 1.31 + i * 0.43) * 0.018 * breath;
          const wob = w1 + w2;
          // A tiny along-edge drift — control point doesn't sit exactly on the
          // perpendicular bisector; it slides along the edge a touch.
          const drift = Math.sin(ph2 * 0.6 + j * 0.43 + i * 2.11) * 0.04 * breath;

          const bow = (bowShallow + (bowDeep - bowShallow) * u + wob) * len * sign;
          const cpx = mx + px * bow + exn * drift * len;
          const cpy = my + py * bow + eyn * drift * len;
          ctx.moveTo(A[0], A[1]);
          ctx.quadraticCurveTo(cpx, cpy, B[0], B[1]);
        }
      }
      ctx.strokeStyle = `rgba(${col},${alpha})`;
      ctx.lineWidth = lw;
      ctx.stroke();
    };

    // Crisp diamond outline (one closed quad) — anchors the silhouette
    const drawOutline = (R, a, alpha, lw) => {
      ctx.beginPath();
      for (let i = 0; i < 4; i++) {
        const x = cx + Math.cos(a + CORNER_ANGLES[i]) * R;
        const y = cy + Math.sin(a + CORNER_ANGLES[i]) * R;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      ctx.closePath();
      ctx.strokeStyle = `rgba(212,168,67,${alpha})`;
      ctx.lineWidth = lw;
      ctx.stroke();
    };

    // ---- Frame loop with 30fps cap on mobile, visibility pause
    let t0 = performance.now();
    let lastFrame = 0;
    let isVisible = true;
    let rafId = null;

    // Breath: ~11.4 seconds per full cycle (0 → 1 → 0) — slow, deliberate
    const BREATH_OMEGA = 0.00055;

    const drawFrame = (now) => {
      rafId = null;
      if (!isVisible || document.hidden) return;

      if (!reduced && now - lastFrame < TARGET_MS - 1) {
        rafId = requestAnimationFrame(drawFrame);
        return;
      }
      lastFrame = now;
      const t = now - t0;

      // breath: 0 (exhaled, dim) → 1 (inhaled, bright). Not linear — eased.
      const raw = (Math.sin(t * BREATH_OMEGA) + 1) * 0.5;
      const breath = raw * raw * (3 - 2 * raw);            // smoothstep
      const scale = 1 + breath * 0.05;                      // 1.00 → 1.05
      const alphaMult = 0.32 + breath * 0.68;               // 0.32 → 1.00 (clear pulse)
      const blur = TIER === 'high' ? (8 + breath * 18) : 0; // 8 → 26 px on desktop

      ctx.clearRect(0, 0, w, h);

      // Vignette — keeps the title legible. Always centered on the text,
      // regardless of where the parallax pushes the figure.
      const grd = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.min(w, h) * 0.55);
      grd.addColorStop(0,    'rgba(10,9,8,0.86)');
      grd.addColorStop(0.55, 'rgba(10,9,8,0.40)');
      grd.addColorStop(1,    'rgba(10,9,8,0.00)');
      ctx.fillStyle = grd;
      ctx.fillRect(0, 0, w, h);

      // 0.46 keeps the whole diamond (plus glints and the breath swell)
      // inside the frame at any aspect ratio — no clipped corners.
      const baseR = Math.min(w, h) * 0.46 * scale;

      // Parallax with inertia — ease toward the cursor, never snap.
      par.x += (parTarget.x - par.x) * 0.045;
      par.y += (parTarget.y - par.y) * 0.045;

      // Tilt: a slow idle sway (~±1.1°) plus a lean toward the cursor.
      const tilt = Math.sin(t * 0.00006) * 0.02 + par.x * 0.022;

      // Shift the figure (not the vignette) a few px toward the cursor.
      const baseCx = cx, baseCy = cy;
      cx = baseCx + par.x * 12;
      cy = baseCy + par.y * 9;

      // Glow — desktop only, intensifies with breath
      if (blur > 0) {
        ctx.shadowColor = 'rgba(212,168,67,0.95)';
        ctx.shadowBlur = blur;
      } else {
        ctx.shadowBlur = 0;
      }

      // Single outer-corner outline — all filaments anchor here.
      drawOutline(baseR * 1.03, tilt, 0.20 * alphaMult, 0.85);
      drawOutline(baseR * 1.00, tilt, 0.12 * alphaMult, 0.45);

      // Filament bundles — every one connects the SAME 4 outer corners.
      // Each bundle has an overlapping bow range; together they read as a
      // unified breathing veil rather than concentric rings.
      for (let i = 0; i < layers.length; i++) {
        const L = layers[i];
        const bowDeep = L.baseDepth + (L.peakDepth - L.baseDepth) * breath;
        drawDiamond(baseR, tilt, L.N, L.alpha * alphaMult, L.lw, L.bowFrom, bowDeep, t, breath, L.col);
      }

      // Corner glints — small luminous points at the 4 shared anchors,
      // brightening on the inhale. They make the anchor structure legible.
      const glintA = 0.22 + breath * 0.55;
      const glintR = 1.1 + breath * 0.9;
      ctx.fillStyle = `rgba(234,198,112,${glintA})`;
      for (let i = 0; i < 4; i++) {
        const gx = cx + Math.cos(tilt + CORNER_ANGLES[i]) * baseR * 1.03;
        const gy = cy + Math.sin(tilt + CORNER_ANGLES[i]) * baseR * 1.03;
        ctx.beginPath();
        ctx.arc(gx, gy, glintR, 0, Math.PI * 2);
        ctx.fill();
      }

      // Reset shadow so it doesn't leak into the next frame's clear,
      // and restore the true center for the next vignette pass.
      ctx.shadowBlur = 0;
      cx = baseCx;
      cy = baseCy;

      // For reduced-motion users we render once; don't schedule another frame.
      if (!reduced) {
        rafId = requestAnimationFrame(drawFrame);
      }
    };

    const start = () => {
      if (rafId === null && isVisible && !document.hidden && !reduced) {
        rafId = requestAnimationFrame(drawFrame);
      }
    };

    // Pause when offscreen
    if ('IntersectionObserver' in window) {
      const io = new IntersectionObserver(([entry]) => {
        isVisible = entry.isIntersecting;
        if (isVisible) start();
      }, { rootMargin: '120px' });
      io.observe(canvas);
    }

    // Pause when tab is hidden
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) start();
    });

    if (reduced) {
      // Static frame for reduced-motion users
      drawFrame(performance.now());
    } else {
      start();
    }
  }

  /* -----------------------------------------
     2. Reveal-on-scroll
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
     3. Topbar — scrolled state
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
     4. State machine readout
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
     5. Codex tabs — SOUL / RULES / Skill
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
     5b. Hero — rotating example prompts
     ----------------------------------------- */

  const promptEl = document.getElementById('hero-prompt-text');
  if (promptEl && !matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const PROMPTS = [
      'build my morning brief before I wake',
      'research this and have a report ready by lunch',
      'transcribe this voice memo into notes',
      'watch my feeds — ping my phone if something matters',
      'remember how I like my summaries',
    ];
    let pi = 0;
    const TYPE_MS = 42, ERASE_MS = 16, HOLD_MS = 2600;

    const tick = (fn, ms) => setTimeout(() => {
      // Idle quietly while the tab is hidden
      if (document.hidden) { tick(fn, 600); return; }
      fn();
    }, ms);

    const erase = () => {
      const cur = promptEl.textContent;
      if (cur.length === 0) {
        pi = (pi + 1) % PROMPTS.length;
        tick(type, 300);
      } else {
        promptEl.textContent = cur.slice(0, -1);
        tick(erase, ERASE_MS);
      }
    };
    const type = () => {
      const target = PROMPTS[pi];
      const cur = promptEl.textContent;
      if (cur.length >= target.length) {
        tick(erase, HOLD_MS);
      } else {
        promptEl.textContent = target.slice(0, cur.length + 1);
        tick(type, TYPE_MS);
      }
    };
    tick(erase, HOLD_MS); // start from the prerendered first prompt
  }

  /* -----------------------------------------
     6. Pipeline event-log readout
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
     7. API code-sample language tabs
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
