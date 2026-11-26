/* Ядро Кимо: сфера из частиц, три орбитальных кольца и мягкое свечение. Пульсация зависит от активности агентов (0..1). */
(function () {
  'use strict';
  const reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const N = 260;
  const pts = [];
  for (let i = 0; i < N; i++) {              // сфера Фибоначчи
    const y = 1 - (i / (N - 1)) * 2, r = Math.sqrt(1 - y * y), a = i * 2.399963;
    pts.push([Math.cos(a) * r, y, Math.sin(a) * r]);
  }

  function mount(canvas, getState) {
    const ctx = canvas.getContext('2d');
    let w = 0, h = 0, dpr = 1, raf = 0, t0 = performance.now(), accent = [25, 211, 197], pulse = 0;

    function readAccent() {
      const v = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
      const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(v);
      if (m) accent = [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)];
    }
    function size() {
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = canvas.clientWidth; h = canvas.clientHeight;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    const rgba = (a) => `rgba(${accent[0]},${accent[1]},${accent[2]},${a})`;

    function frame(now) {
      const time = (now - t0) / 1000;
      const act = Math.max(0, Math.min(1, (getState() || {}).activity || 0));
      const speed = 0.25 + act * 0.9;
      pulse += ((0.5 + 0.5 * Math.sin(time * (1.1 + act * 2.2))) - pulse) * 0.2;
      const cx = w / 2, cy = h / 2, R = Math.min(w, h) * (0.25 + 0.012 * pulse + 0.016 * act * pulse);
      ctx.clearRect(0, 0, w, h);

      const g = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, Math.min(w, h) * 0.5);
      g.addColorStop(0, rgba(0.20 + 0.14 * pulse + 0.1 * act)); g.addColorStop(1, rgba(0));
      ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);

      const ay = time * speed, ax = 0.45 + Math.sin(time * 0.3) * 0.12;
      const cy_ = Math.cos(ay), sy = Math.sin(ay), cx_ = Math.cos(ax), sx = Math.sin(ax);
      const proj = [];
      for (const p of pts) {
        let x = p[0] * cy_ + p[2] * sy, z = -p[0] * sy + p[2] * cy_, y = p[1];
        const y2 = y * cx_ - z * sx; z = y * sx + z * cx_; y = y2;
        const k = 1 / (1.9 - z * 0.55);
        proj.push([cx + x * R * k * 1.7, cy + y * R * k * 1.7, z]);
      }
      proj.sort((a, b) => a[2] - b[2]);
      for (const q of proj) {
        const depth = (q[2] + 1) / 2;
        ctx.fillStyle = rgba(0.12 + depth * (0.7 + 0.2 * pulse));
        ctx.beginPath(); ctx.arc(q[0], q[1], 0.7 + depth * 1.5, 0, 6.283); ctx.fill();
      }

      ctx.lineWidth = 1.2;
      for (let i = 0; i < 3; i++) {             // кольца
        ctx.save(); ctx.translate(cx, cy);
        ctx.rotate(time * speed * (0.5 + i * 0.35) * (i % 2 ? -1 : 1) + i * 1.05);
        ctx.scale(1, 0.32 + i * 0.12);
        ctx.strokeStyle = rgba(0.22 + 0.2 * pulse - i * 0.04);
        ctx.setLineDash(i === 1 ? [3, 7] : []);
        ctx.beginPath(); ctx.arc(0, 0, R * (1.3 + i * 0.16), 0, 6.283); ctx.stroke();
        ctx.restore();
      }
      ctx.setLineDash([]);
      const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * 0.55);   // сердцевина
      core.addColorStop(0, rgba(0.55 + 0.35 * pulse)); core.addColorStop(1, rgba(0));
      ctx.fillStyle = core; ctx.beginPath(); ctx.arc(cx, cy, R * 0.55, 0, 6.283); ctx.fill();
      if (!reduce) raf = requestAnimationFrame(frame);
    }
    const ro = new ResizeObserver(() => { size(); if (reduce) frame(performance.now()); });
    ro.observe(canvas);
    const onVis = () => { cancelAnimationFrame(raf); if (!document.hidden && !reduce) raf = requestAnimationFrame(frame); };
    document.addEventListener('visibilitychange', onVis);
    readAccent(); size(); raf = requestAnimationFrame(frame);
    return {
      refreshColor: readAccent,
      destroy() { cancelAnimationFrame(raf); ro.disconnect(); document.removeEventListener('visibilitychange', onVis); },
    };
  }
  window.KimoCore = { mount };
})();
