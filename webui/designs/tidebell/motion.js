/* 生图分层素材与阻尼摆动；铃声在用户轻触后合成。 */
(function () {
  "use strict";

  const field = document.querySelector(".sakura-field");
  const fragment = document.createDocumentFragment();
  for (let i = 0; i < 18; i++) {
    const petal = document.createElement("span");
    petal.className = "sakura-fall";
    const values = {
      "--petal-x": ((i * 47 + 9) % 103) + "%",
      "--petal-size": (13 + (i * 7) % 14) + "px",
      "--petal-duration": (25 + (i * 11) % 19) + "s",
      "--petal-delay": -(i * 3.7 + 4) + "s",
      "--petal-drift": (-75 - (i * 17) % 115) + "px",
      "--petal-opacity": String(.2 + (i % 5) * .035),
      "--flutter-duration": (3.8 + (i % 5) * .8) + "s",
    };
    Object.entries(values).forEach(([key, value]) => petal.style.setProperty(key, value));
    petal.appendChild(document.createElement("i"));
    fragment.appendChild(petal);
  }
  field.appendChild(fragment);

  const bell = document.querySelector(".windchime");
  const parts = {
    shell: { node: bell.querySelector(".windchime-rig"), angle: 0, velocity: 0, stiffness: 7, damping: 3.7 },
    clapper: { node: bell.querySelector(".windchime-chain"), angle: 0, velocity: 0, stiffness: 28, damping: 3.6 },
    paper: { node: bell.querySelector(".windchime-paper-joint"), angle: 0, velocity: 0, stiffness: 9, damping: 2.1 },
    tip: { node: bell.querySelector(".windchime-paper-lower"), angle: 0, velocity: 0, stiffness: 35, damping: 3.7 },
  };
  const hint = bell.querySelector(".windchime-hint");
  const announcement = document.querySelector("#windchimeAnnouncement");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let audioContext;
  let output;
  let motionFrame = 0;
  let lastFrame = 0;
  let windTime = 0;
  let bellVisible = true;
  let hintTimer;
  let lastRing = -Infinity;

  function advance(part, target, dt) {
    part.velocity += ((target - part.angle) * part.stiffness - part.velocity * part.damping) * dt;
    part.angle += part.velocity * dt;
    part.node.style.transform = "rotate(" + part.angle.toFixed(3) + "deg)";
  }

  function sway(time) {
    const dt = lastFrame ? Math.min((time - lastFrame) / 1000, 1 / 30) : 1 / 60;
    lastFrame = time;
    windTime += dt;
    const wind = .72 * Math.sin(windTime * .63) + .26 * Math.sin(windTime * 1.19 + 1.7)
      + .12 * Math.sin(windTime * 2.67 + 3.2);
    // 纸签先受风，牵动铃舌；较重的铃罩轻微滞后，纸尾另有柔软回摆。
    advance(parts.paper, wind * 8.5 + Math.sin(windTime * 3.8) * .6, dt);
    advance(parts.clapper, wind * 1.2 + parts.paper.angle * .2, dt);
    advance(parts.shell, wind * .55 + parts.clapper.angle * .08, dt);
    advance(parts.tip, wind * 1.2 - parts.paper.velocity * .11 + Math.sin(windTime * 4.1) * .65, dt);
    motionFrame = requestAnimationFrame(sway);
  }

  function excitePendulum() {
    if (reducedMotion.matches || document.body.classList.contains("calm") || document.body.classList.contains("paused")) return;
    // 施加速度增量，连续点击也从当前姿态继续，不重置角度。
    parts.paper.velocity = Math.min(parts.paper.velocity + 44, 70);
    parts.clapper.velocity = Math.max(parts.clapper.velocity - 25, -60);
    parts.shell.velocity = Math.min(parts.shell.velocity + 3, 9);
    parts.tip.velocity = Math.max(parts.tip.velocity - 20, -60);
  }

  function strike(time, frequency, volume) {
    // 玻璃铃的非整数泛音与各自衰减，避免电子提示音般的齐整音色。
    const partials = [[1, 1, 3.8], [2.756, .34, 2.4], [5.404, .11, 1.25], [8.933, .025, .55]];
    for (const [ratio, strength, decay] of partials) {
      const oscillator = audioContext.createOscillator();
      const envelope = audioContext.createGain();
      oscillator.type = "sine";
      oscillator.frequency.value = frequency * ratio;
      envelope.gain.setValueAtTime(0, time);
      envelope.gain.linearRampToValueAtTime(volume * strength, time + .004);
      envelope.gain.exponentialRampToValueAtTime(.0001, time + decay);
      oscillator.connect(envelope);
      envelope.connect(output);
      oscillator.start(time);
      oscillator.stop(time + decay + .03);
      oscillator.onended = () => {
        oscillator.disconnect();
        envelope.disconnect();
      };
    }
  }

  async function ring() {
    if (performance.now() - lastRing < 220) return;
    lastRing = performance.now();
    excitePendulum();
    if (!audioContext) {
      audioContext = new AudioContext();
      const compressor = audioContext.createDynamicsCompressor();
      compressor.threshold.value = -16;
      compressor.ratio.value = 4;
      output = audioContext.createGain();
      output.gain.value = .32;
      output.connect(compressor);
      compressor.connect(audioContext.destination);
    }
    if (audioContext.state === "suspended") await audioContext.resume();
    const now = audioContext.currentTime;
    const pitch = 1320 * (1 + (Math.random() - .5) * .016);
    strike(now, pitch, .3);
    strike(now + .21, pitch * 1.498, .095);
    announcement.textContent = "风铃轻响";
    hint.textContent = "铃声随风，记忆生长";
    bell.classList.add("is-ringing");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => {
      bell.classList.remove("is-ringing");
      hint.textContent = "轻触听铃";
    }, 2200);
  }

  bell.addEventListener("click", () => {
    ring().catch(() => {
      announcement.textContent = "声音暂未播放，请再轻触一次风铃";
      hint.textContent = "再轻触一次听铃";
    });
  });

  function updateMotion() {
    const hidden = document.hidden;
    if (document.body.classList.contains("motion-idle") !== hidden) {
      document.body.classList.toggle("motion-idle", hidden);
    }
    const calm = reducedMotion.matches || document.body.classList.contains("calm");
    const stopped = hidden || calm || !bellVisible || document.body.classList.contains("paused");
    if (stopped) {
      cancelAnimationFrame(motionFrame);
      motionFrame = 0;
      lastFrame = 0;
      if (calm) {
        Object.values(parts).forEach(part => {
          part.angle = 0;
          part.velocity = 0;
          part.node.style.transform = "rotate(0deg)";
        });
      }
    } else if (!motionFrame) {
      motionFrame = requestAnimationFrame(sway);
    }
    if (hidden && audioContext && audioContext.state === "running") {
      audioContext.suspend().catch(() => {
        announcement.textContent = "可在返回页面后再次轻触风铃";
      });
    }
  }
  document.addEventListener("visibilitychange", updateMotion);
  reducedMotion.addEventListener("change", updateMotion);
  new IntersectionObserver(([entry]) => {
    bellVisible = entry.isIntersecting;
    updateMotion();
  }).observe(bell);
  new MutationObserver(updateMotion).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  updateMotion();
})();
