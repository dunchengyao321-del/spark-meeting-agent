(() => {
  const BASE = "http://127.0.0.1:8765";
  const ENDPOINT = BASE + "/api/feishu/page_state";
  const AUDIO_URL = BASE + "/api/feishu/next_audio";
  const TAG = "[星火桥接]";

  // ------------------------------------------------------------ 页面状态上报（原有）
  async function report() {
    try {
      const body = JSON.stringify({
        url: location.href,
        title: document.title,
        text: (document.body ? document.body.innerText : "").slice(0, 3000),
      });
      const resp = await fetch(ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        keepalive: true,
      });
      console.log(TAG, "上报成功", resp.status, location.href);
    } catch (err) {
      console.warn(TAG, "上报失败", String(err), location.href);
    }
  }

  // ------------------------------------------------------------ 音频注入（方案 A）
  // 用一个页面级 AudioContext + 注入队列，把服务端推来的 TTS PCM 播进会议麦克风轨道。
  let audioCtx = null;
  let injectDest = null;      // MediaStreamDestination：注入音轨的目的地
  let micStream = null;       // 真实麦克风流
  let mixedTrack = null;      // 混音后的轨道（喂给飞书）
  let hooked = false;

  function ensureCtx() {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
      injectDest = audioCtx.createMediaStreamDestination();
    }
    if (audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
    return audioCtx;
  }

  // 把 24kHz 单声道 PCM16 播进注入目的地（进会议麦克风）
  function playPcm(pcmBytes) {
    try {
      const ctx = ensureCtx();
      const n = pcmBytes.length / 2;
      const buf = ctx.createBuffer(1, n, 24000);
      const ch = buf.getChannelData(0);
      const dv = new DataView(pcmBytes);
      for (let i = 0; i < n; i++) ch[i] = dv.getInt16(i * 2, true) / 32768;
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(injectDest);
      src.start();
      console.log(TAG, "已注入音频到会议麦克风", n, "采样");
    } catch (e) {
      console.warn(TAG, "注入音频失败", String(e));
    }
  }

  // 轮询服务端取待播放音频（PCM 二进制）
  async function pollAudio() {
    try {
      const r = await fetch(AUDIO_URL, { cache: "no-store" });
      if (r.status === 200) {
        const ab = await r.arrayBuffer();
        if (ab.byteLength > 0) playPcm(ab);
      }
    } catch (e) { /* 静默 */ }
    setTimeout(pollAudio, 500);
  }

  // 拦截 getUserMedia：返回「麦克风 + 注入音」的混合流，让飞书采到我们的 TTS
  function hookGetUserMedia() {
    if (hooked) return;
    hooked = true;
    const orig = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
    navigator.mediaDevices.getUserMedia = async function (constraints) {
      const stream = await orig(constraints);
      if (!constraints || !constraints.audio) return stream;
      try {
        const ctx = ensureCtx();
        micStream = stream;
        const micSrc = ctx.createMediaStreamSource(stream);
        const mixDest = ctx.createMediaStreamDestination();
        // 麦克风 → 混合目的地
        micSrc.connect(mixDest);
        // 注入音 → 混合目的地（injectDest 的音也接到 mixDest）
        injectDest.connect(mixDest);
        const track = mixDest.stream.getAudioTracks()[0];
        if (track) {
          // 用混合轨道替换原麦克风轨道
          mixedTrack = track;
          const newStream = new MediaStream(stream.getTracks().filter(t => t.kind !== "audio"));
          newStream.addTrack(track);
          console.log(TAG, "已接管 getUserMedia，返回混合音轨");
          return newStream;
        }
      } catch (e) {
        console.warn(TAG, "混合音轨失败，回退原麦克风", String(e));
      }
      return stream;
    };
    console.log(TAG, "getUserMedia 钩子已安装");
  }

  console.log(TAG, "内容脚本已注入", location.href);
  report();
  setInterval(report, 3000);
  hookGetUserMedia();
  pollAudio();
})();
