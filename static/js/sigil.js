// Pernix sigil — animated string-art diamond.
// Ported from the pernix-website hero canvas. Self-contained: handles DPR,
// resize, IntersectionObserver pause, tab-hidden pause, prefers-reduced-motion.
//
// Usage:
//   const stop = initSigil(canvasEl);
//   // ...later, when removing the canvas:
//   stop();   // optional — natural pause kicks in when offscreen anyway

import { readColor, isLight } from './theme.js';

export function initSigil(canvas) {
    if (!canvas) return () => {};
    const ctx = canvas.getContext('2d', { alpha: true });

    // The filaments used to be a hard-coded rgba(212,168,67,…) — the dark
    // theme's gold, at alphas between 0.12 and 0.42. On paper that is a
    // near-invisible smear: the whole point of a low alpha is that the page
    // shows through, and a white page shows through gold completely. Read the
    // accent from the live palette, and lift every alpha on a light ground so
    // the same drawing reads with the same weight.
    let INK = [212, 168, 67];   // last-resort fallback if --accent is unreadable
    let ALPHA_LIFT = 1;
    let GLOW = '';

    const readPalette = () => {
        INK = readColor('--accent', [212, 168, 67]).slice(0, 3);
        // On paper the canvas blends with `multiply` (see --sigil-blend), which
        // darkens rather than lightens, so the same alphas land much closer to
        // the intended weight — a smaller lift than a straight overlay would
        // need, and a far quieter glow.
        const paper = isLight();
        ALPHA_LIFT = paper ? 1.45 : 1;
        GLOW = `rgba(${INK[0]},${INK[1]},${INK[2]},${paper ? 0.30 : 0.95})`;
    };
    const stroke = (alpha) =>
        `rgba(${INK[0]},${INK[1]},${INK[2]},${Math.min(1, alpha * ALPHA_LIFT)})`;
    readPalette();

    // Performance tiering — fewer strings + lower DPR + no glow on mobile/low-power
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const small = matchMedia('(max-width: 768px)').matches;
    const cores = navigator.hardwareConcurrency || 2;
    // The UA test misses an iPad in desktop mode — it says "Macintosh" there,
    // and iPads report 8+ cores, so tablets landed on the 60fps/high-filament
    // tier and ran hot. data-touch-ui (touch-boot.js) does see them.
    const lowPower = small || cores <= 4 ||
        document.documentElement.hasAttribute('data-touch-ui') ||
        /Android|webOS|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);
    const TIER = lowPower ? 'low' : 'high';
    const MAX_DPR = TIER === 'low' ? 1.5 : 2;
    const TARGET_MS = TIER === 'low' ? 33 : 16;  // 30fps mobile, 60fps desktop

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
    const onResize = () => {
        clearTimeout(resizeT);
        resizeT = setTimeout(resize, 120);
    };
    window.addEventListener('resize', onResize, { passive: true });

    // Filament bundles — every line connects two adjacent OUTER corners.
    // The "depth" feel comes from multiple bundles with overlapping bow ranges.
    const layers = TIER === 'low' ? [
        { N: 14, alpha: 0.34, lw: 0.45, bowFrom: -0.005, baseDepth: -0.08, peakDepth: -0.18 },
        { N: 16, alpha: 0.40, lw: 0.45, bowFrom: -0.04,  baseDepth: -0.16, peakDepth: -0.28 },
        { N: 12, alpha: 0.30, lw: 0.40, bowFrom: -0.10,  baseDepth: -0.22, peakDepth: -0.36 },
    ] : [
        { N: 22, alpha: 0.36, lw: 0.45, bowFrom: -0.005, baseDepth: -0.08, peakDepth: -0.18 },
        { N: 26, alpha: 0.42, lw: 0.45, bowFrom: -0.04,  baseDepth: -0.16, peakDepth: -0.30 },
        { N: 22, alpha: 0.32, lw: 0.40, bowFrom: -0.10,  baseDepth: -0.22, peakDepth: -0.38 },
    ];

    const CORNER_ANGLES = [0, Math.PI / 2, Math.PI, 3 * Math.PI / 2];

    // Bundle of bowing curves connecting the 4 outer corners.
    const drawDiamond = (R, a, N, alpha, lw, bowShallow, bowDeep, t, breath) => {
        const cs = [
            [cx + Math.cos(a + CORNER_ANGLES[0]) * R, cy + Math.sin(a + CORNER_ANGLES[0]) * R],
            [cx + Math.cos(a + CORNER_ANGLES[1]) * R, cy + Math.sin(a + CORNER_ANGLES[1]) * R],
            [cx + Math.cos(a + CORNER_ANGLES[2]) * R, cy + Math.sin(a + CORNER_ANGLES[2]) * R],
            [cx + Math.cos(a + CORNER_ANGLES[3]) * R, cy + Math.sin(a + CORNER_ANGLES[3]) * R],
        ];

        const ph1 = t * 0.00065;
        const ph2 = t * 0.00112;
        const chaos = 0.4 + breath * 0.8;

        ctx.beginPath();
        for (let i = 0; i < 4; i++) {
            const A = cs[i];
            const B = cs[(i + 1) & 3];
            const mx = (A[0] + B[0]) * 0.5;
            const my = (A[1] + B[1]) * 0.5;
            const ex = B[0] - A[0];
            const ey = B[1] - A[1];
            const len = Math.sqrt(ex * ex + ey * ey);
            const exn = ex / len, eyn = ey / len;
            const px = -eyn,    py = exn;
            const sign = ((mx - cx) * px + (my - cy) * py) > 0 ? 1 : -1;

            const invN = N === 1 ? 0 : 1 / (N - 1);
            for (let j = 0; j < N; j++) {
                const u = N === 1 ? 0.5 : j * invN;
                const w1 = Math.sin(ph1 + j * 0.71 + i * 1.37) * 0.012 * chaos;
                const w2 = Math.sin(ph2 * 1.7 + j * 1.31 + i * 0.43) * 0.018 * breath;
                const wob = w1 + w2;
                const drift = Math.sin(ph2 * 0.6 + j * 0.43 + i * 2.11) * 0.04 * breath;

                const bow = (bowShallow + (bowDeep - bowShallow) * u + wob) * len * sign;
                const cpx = mx + px * bow + exn * drift * len;
                const cpy = my + py * bow + eyn * drift * len;
                ctx.moveTo(A[0], A[1]);
                ctx.quadraticCurveTo(cpx, cpy, B[0], B[1]);
            }
        }
        ctx.strokeStyle = stroke(alpha);
        ctx.lineWidth = lw;
        ctx.stroke();
    };

    // Crisp closed diamond outline — anchors the silhouette.
    const drawOutline = (R, a, alpha, lw) => {
        ctx.beginPath();
        for (let i = 0; i < 4; i++) {
            const x = cx + Math.cos(a + CORNER_ANGLES[i]) * R;
            const y = cy + Math.sin(a + CORNER_ANGLES[i]) * R;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.strokeStyle = stroke(alpha);
        ctx.lineWidth = lw;
        ctx.stroke();
    };

    let t0 = performance.now();
    let lastFrame = 0;
    let isVisible = true;
    let rafId = null;
    let stopped = false;

    // Breath: ~11.4s per full cycle (0 → 1 → 0). Slow, deliberate.
    const BREATH_OMEGA = 0.00055;

    const drawFrame = (now) => {
        rafId = null;
        if (stopped || !isVisible || document.hidden) return;

        if (!reduced && now - lastFrame < TARGET_MS - 1) {
            rafId = requestAnimationFrame(drawFrame);
            return;
        }
        lastFrame = now;
        const t = now - t0;

        const raw = (Math.sin(t * BREATH_OMEGA) + 1) * 0.5;
        const breath = raw * raw * (3 - 2 * raw);          // smoothstep
        const scale = 1 + breath * 0.05;                    // 1.00 → 1.05
        const alphaMult = 0.32 + breath * 0.68;             // 0.32 → 1.00
        const blur = TIER === 'high' ? (8 + breath * 18) : 0;

        ctx.clearRect(0, 0, w, h);

        // Standalone use (no text behind), so leave a touch more padding than
        // the website hero (which uses 0.52 to fill the frame). 0.44 keeps the
        // outermost filaments comfortably inside the canvas at peak inhale.
        const baseR = Math.min(w, h) * 0.44 * scale;

        if (blur > 0) {
            ctx.shadowColor = GLOW;
            ctx.shadowBlur = blur;
        } else {
            ctx.shadowBlur = 0;
        }

        drawOutline(baseR * 1.03, 0, 0.20 * alphaMult, 0.85);
        drawOutline(baseR * 1.00, 0, 0.12 * alphaMult, 0.45);

        for (let i = 0; i < layers.length; i++) {
            const L = layers[i];
            const bowDeep = L.baseDepth + (L.peakDepth - L.baseDepth) * breath;
            drawDiamond(baseR, 0, L.N, L.alpha * alphaMult, L.lw, L.bowFrom, bowDeep, t, breath);
        }

        ctx.shadowBlur = 0;

        if (!reduced) {
            rafId = requestAnimationFrame(drawFrame);
        }
    };

    const start = () => {
        if (stopped) return;
        if (rafId === null && isVisible && !document.hidden && !reduced) {
            rafId = requestAnimationFrame(drawFrame);
        }
    };

    let io = null;
    if ('IntersectionObserver' in window) {
        io = new IntersectionObserver(([entry]) => {
            isVisible = entry.isIntersecting;
            if (isVisible) start();
        }, { rootMargin: '120px' });
        io.observe(canvas);
    }

    const onVisibilityChange = () => {
        if (!document.hidden) start();
    };
    document.addEventListener('visibilitychange', onVisibilityChange);

    // The theme can change under a running canvas — from Settings, or because
    // the OS flipped while "System" is selected. Re-read and repaint.
    const onTheme = () => {
        readPalette();
        if (reduced) drawFrame(performance.now());
        else start();
    };
    window.addEventListener('pernix:theme', onTheme);

    if (reduced) {
        drawFrame(performance.now());  // single static frame
    } else {
        start();
    }

    return () => {
        stopped = true;
        if (rafId !== null) {
            cancelAnimationFrame(rafId);
            rafId = null;
        }
        window.removeEventListener('resize', onResize);
        window.removeEventListener('pernix:theme', onTheme);
        document.removeEventListener('visibilitychange', onVisibilityChange);
        if (io) {
            io.disconnect();
            io = null;
        }
    };
}
