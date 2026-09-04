/**
 * shim.js —— 飞书会议语音智能体 · 页面层注入脚本（方案C）
 *
 * 由 Playwright 的 context.add_init_script 在页面任何脚本执行前注入。
 * 职责：
 *   1) 伪造 enumerateDevices 设备列表（逼真的虚拟麦克风/扬声器/摄像头 label）；
 *   2) 拦截 getUserMedia：无论页面请求什么 deviceId/约束，都返回"合成音频轨"，
 *      applyConstraints 直接吞掉不报错；
 *   3) 包装 RTCPeerConnection（保留原型链与静态方法），捕获每条远端音频轨，
 *      经 AudioWorklet 转 Int16 PCM 通过 WebSocket 发往后端（下行，会议室 -> ASR）；
 *   4) WebSocket 收到后端 TTS PCM 后，经抖动缓冲（jitter buffer ~200ms）写入合成麦克风轨，
 *      让会议室所有人听到智能体说话（上行，TTS -> 会议室）；
 *   5) 暴露 window.__agentShim = {状态, 电平, 统计} 供外部探针（run_agent.py）读取。
 *
 * 音频格式统一：48kHz / 单声道 / 20ms 一帧（960 采样）/ Int16 小端 PCM 走 WebSocket。
 *
 * WebSocket 协议（与 bridge_server.py 对应）：
 *   单条连接复用两个逻辑通道，二进制帧第 1 字节为通道号：
 *     0x01 下行 meeting_pcm：浏览器 -> 服务器（会议室声音，喂 ASR）
 *     0x02 上行 tts_pcm    ：服务器 -> 浏览器（TTS 结果，注入麦克风轨）
 *   文本帧为 JSON 控制消息（hello/ping/pong 等）。
 */
(() => {
  'use strict';

  /* ==================== 0. 防重复注入 ==================== */
  if (window.__agentShimInjected) return;
  Object.defineProperty(window, '__agentShimInjected', { value: true });

  /* ==================== 1. 常量 ==================== */
  // 支持外部在注入 shim 之前通过 window.__AGENT_WS_URL 覆盖地址（避开端口占用），默认 8765
  const WS_URL = window.__AGENT_WS_URL || 'ws://127.0.0.1:8765/ws';
  const SAMPLE_RATE = 48000;                          // 统一 48kHz
  const FRAME_MS = 20;                                // 20ms 一帧
  const FRAME_SAMPLES = (SAMPLE_RATE * FRAME_MS) / 1000; // 960 采样/帧
  const JITTER_TARGET_MS = 100;                       // 抖动缓冲目标 ~100ms（打断提速；本地链路稳定，200 属冗余）
  const JITTER_MAX_MS = 1000;                         // 抖动缓冲上限，超出丢最旧
  const CH_DOWNLINK = 0x01;                           // 下行通道号：会议室 -> 后端
  const CH_UPLINK = 0x02;                             // 上行通道号：后端 -> 浏览器

  const TAG = '[AgentShim]';
  const log = (...args) => console.log(TAG, ...args);
  const warn = (...args) => console.warn(TAG, ...args);

  /* ==================== 2. 对外探针（run_agent.py 周期性读取） ==================== */
  const shim = {
    version: '0.1.0',
    state: 'init',          // init | connecting | connected | reconnecting
    wsUrl: WS_URL,
    sampleRate: SAMPLE_RATE,
    micMode: 'none',        // none | generator | worklet（合成麦克风轨的实现路径）
    levels: { downlink: 0, uplink: 0 }, // 0~1 RMS 实时电平
    stats: {
      remoteTracks: 0,        // 已接管的远端音频轨数量
      downlinkFrames: 0,      // 已发送的下行帧数
      downlinkBytes: 0,       // 已发送的下行字节数
      uplinkFrames: 0,        // 已播放的上行帧数
      uplinkBytes: 0,         // 已接收的上行字节数
      uplinkUnderruns: 0,     // 上行欠载次数（缓冲空了，播了静音）
      uplinkDiscards: 0,      // 上行因缓冲超限被丢弃的块数
      wsReconnects: 0,        // WebSocket 重连次数
      startedAt: Date.now(),
    },
  };
  Object.defineProperty(window, '__agentShim', { value: shim, configurable: true });

  /* ==================== 3. 小工具 ==================== */
  // Int16 PCM -> Float32（-1.0 ~ 1.0）
  function int16ToFloat32(i16) {
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    return f32;
  }
  // Float32 -> Int16 并钳位
  function float32ToInt16(f32) {
    const i16 = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
      const v = Math.max(-1, Math.min(1, f32[i]));
      i16[i] = v < 0 ? v * 32768 : v * 32767;
    }
    return i16;
  }
  // 计算 RMS 电平（0~1），用于探针展示
  function rms(f32) {
    let sum = 0;
    for (let i = 0; i < f32.length; i++) sum += f32[i] * f32[i];
    return f32.length ? Math.sqrt(sum / f32.length) : 0;
  }

  /* ==================== 4. AudioWorklet 处理器（内联 Blob URL，避免外部文件依赖） ==================== */
  // 一个 Blob 里同时注册"采集"与"播放"两个处理器
  const WORKLET_SOURCE = `
    // 采集处理器：把输入声道 0 的 Float32 数据原样抛回主线程
    class AgentCaptureProcessor extends AudioWorkletProcessor {
      process(inputs) {
        const input = inputs[0];
        if (input && input[0] && input[0].length) {
          // 拷贝一份再 post，避免底层缓冲被复用
          this.port.postMessage(input[0].slice(0));
        }
        return true;
      }
    }
    registerProcessor('agent-capture', AgentCaptureProcessor);

    // 播放处理器：主线程通过 port 推 Float32 块，这里排队消费，不足补静音
    class AgentPlaybackProcessor extends AudioWorkletProcessor {
      constructor() {
        super();
        this.queue = [];
        this.offset = 0;
        this.port.onmessage = (e) => {
          this.queue.push(new Float32Array(e.data));
        };
      }
      process(inputs, outputs) {
        const out = outputs[0][0];
        let i = 0;
        while (i < out.length) {
          if (!this.queue.length) { // 没数据补静音
            for (let j = i; j < out.length; j++) out[j] = 0;
            break;
          }
          const head = this.queue[0];
          const n = Math.min(out.length - i, head.length - this.offset);
          for (let k = 0; k < n; k++) out[i + k] = head[this.offset + k];
          this.offset += n;
          i += n;
          if (this.offset >= head.length) { this.queue.shift(); this.offset = 0; }
        }
        return true;
      }
    }
    registerProcessor('agent-playback', AgentPlaybackProcessor);
  `;
  let _workletURL = null;
  function workletURL() {
    if (!_workletURL) {
      _workletURL = URL.createObjectURL(new Blob([WORKLET_SOURCE], { type: 'application/javascript' }));
    }
    return _workletURL;
  }

  /* ==================== 5. 抖动缓冲（jitter buffer，~200ms） ==================== */
  class JitterBuffer {
    constructor(targetMs, maxMs) {
      this.targetSamples = (SAMPLE_RATE * targetMs) / 1000;
      this.maxSamples = (SAMPLE_RATE * maxMs) / 1000;
      this.chunks = [];      // Int16Array 队列
      this.samples = 0;      // 当前缓冲的总采样数
      this.started = false;  // 是否已攒够起播水位
    }
    // 收到一块上行 Int16 PCM，入队；超过上限丢最旧（保实时性）
    push(i16) {
      this.chunks.push(i16);
      this.samples += i16.length;
      shim.stats.uplinkBytes += i16.byteLength;
      while (this.samples > this.maxSamples && this.chunks.length) {
        const dropped = this.chunks.shift();
        this.samples -= dropped.length;
        shim.stats.uplinkDiscards++;
      }
      if (!this.started && this.samples >= this.targetSamples) {
        this.started = true; // 攒够 ~200ms 才允许出声，抗网络抖动
      }
    }
    // 清空缓冲（收到 clear_audio 打断指令时调用）：立即停声，重新起播
    reset() {
      this.chunks = [];
      this.samples = 0;
      this.started = false;
    }
    // 每 20ms 拉一帧；未起播或欠载时吐静音
    pull(n) {
      const out = new Int16Array(n);
      if (!this.started) return out;
      let off = 0;
      while (off < n && this.chunks.length) {
        const head = this.chunks[0];
        const take = Math.min(n - off, head.length);
        out.set(head.subarray(0, take), off);
        off += take;
        if (take === head.length) this.chunks.shift();
        else this.chunks[0] = head.subarray(take);
      }
      this.samples -= off;
      if (off < n) {
        shim.stats.uplinkUnderruns++;
        this.started = false; // 欠载后重新缓冲到水位再播
      }
      return out;
    }
  }

  const jitter = new JitterBuffer(JITTER_TARGET_MS, JITTER_MAX_MS);

  /* ==================== 6. 合成麦克风轨（上行播放链路） ==================== */
  // 给合成轨打补丁：吞掉 applyConstraints、伪造 settings/capabilities、接管 stop 清理
  function patchTrack(track, onStop) {
    // 伪装轨 label：合成轨默认 label 是 UUID，飞书设备列表会露馅，覆盖为逼真名称
    try {
      Object.defineProperty(track, 'label', { value: 'Microphone (HD Audio Device)', configurable: true });
    } catch (_) { /* 个别内核不允许重定义则忽略 */ }
    track.applyConstraints = async () => { /* 吞掉约束，直接成功不报错 */ };
    track.getSettings = () => ({
      deviceId: 'agent-mic-01', groupId: 'agent-group-audio',
      sampleRate: SAMPLE_RATE, sampleSize: 16, channelCount: 1,
      echoCancellation: false, noiseSuppression: false, autoGainControl: false, latency: 0.01,
    });
    track.getCapabilities = () => ({
      deviceId: 'agent-mic-01',
      sampleRate: { min: SAMPLE_RATE, max: SAMPLE_RATE },
      channelCount: { min: 1, max: 1 },
      echoCancellation: [false],
    });
    track.getConstraints = () => ({});
    const origStop = track.stop.bind(track);
    track.stop = () => {
      try { onStop && onStop(); } catch (_) { /* 忽略清理异常 */ }
      origStop();
    };
    return track;
  }

  // 每 20ms 从抖动缓冲拉一帧，更新上行电平统计
  function pullUplinkFrame() {
    const i16 = jitter.pull(FRAME_SAMPLES);
    const f32 = int16ToFloat32(i16);
    shim.levels.uplink = rms(f32);
    shim.stats.uplinkFrames++;
    return { i16, f32 };
  }

  // 创建合成音频轨：优先 MediaStreamTrackGenerator + WebCodecs，降级 AudioContext + AudioWorklet
  let _syntheticTrackPromise = null;
  function getSyntheticTrack() {
    if (_syntheticTrackPromise) {
      // 飞书会探测性 stop 轨（拿到→探测→停止→重新申请）：
      // 缓存轨若已 ended 必须丢弃重建，否则上行定时器已被清理，永远发不出声
      return _syntheticTrackPromise.then((t) => {
        if (t.readyState === 'live') return t;
        _syntheticTrackPromise = null;
        return getSyntheticTrack();
      });
    }
    _syntheticTrackPromise = createSyntheticTrack();
    return _syntheticTrackPromise;
  }
  async function createSyntheticTrack() {
    // ---- 路径一：MediaStreamTrackGenerator + WebCodecs AudioData（低延迟，Chrome 94+）----
    if (typeof MediaStreamTrackGenerator !== 'undefined' && typeof AudioData !== 'undefined') {
      try {
        const gen = new MediaStreamTrackGenerator({ kind: 'audio' });
        const writer = gen.writable.getWriter();
        let timestamp = 0; // 微秒时间戳，逐帧递增
        const timer = setInterval(async () => {
          const { f32 } = pullUplinkFrame();
          const data = new AudioData({
            format: 'f32',                 // 48kHz 单声道 Float32
            sampleRate: SAMPLE_RATE,
            numberOfChannels: 1,
            numberOfFrames: FRAME_SAMPLES,
            timestamp,
            data: f32.buffer,
          });
          timestamp += (FRAME_SAMPLES / SAMPLE_RATE) * 1e6;
          try {
            await writer.write(data);
          } catch (e) {
            warn('写入合成轨失败:', e);
          } finally {
            data.close(); // AudioData 写完必须关闭释放底层缓冲
          }
        }, FRAME_MS);
        shim.micMode = 'generator';
        log('合成麦克风轨：MediaStreamTrackGenerator + WebCodecs');
        return patchTrack(gen, () => { clearInterval(timer); writer.close().catch(() => {}); });
      } catch (e) {
        warn('TrackGenerator 路径失败，降级 AudioWorklet:', e);
      }
    }

    // ---- 路径二（降级）：AudioContext.createMediaStreamDestination + AudioWorklet ----
    const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
    await ctx.audioWorklet.addModule(workletURL());
    try { await ctx.resume(); } catch (_) { /* 自动播放策略由启动 flags 放开 */ }
    const dest = ctx.createMediaStreamDestination();
    const node = new AudioWorkletNode(ctx, 'agent-playback', {
      numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1],
    });
    node.connect(dest);
    const timer = setInterval(() => {
      const { f32 } = pullUplinkFrame();
      // transfer 方式把缓冲交给 worklet 线程
      node.port.postMessage(f32.buffer, [f32.buffer]);
    }, FRAME_MS);
    const track = dest.stream.getAudioTracks()[0];
    shim.micMode = 'worklet';
    log('合成麦克风轨：AudioContext + AudioWorklet（降级路径）');
    return patchTrack(track, () => { clearInterval(timer); ctx.close().catch(() => {}); });
  }

  /* ==================== 7. 下行采集链路（远端轨 -> PCM -> WebSocket） ==================== */
  // 把 AudioWorklet 抛回的 128 采样小块拼成 960 采样（20ms）帧再发送
  class DownlinkEncoder {
    constructor() { this.buf = new Float32Array(0); }
    push(block) {
      const merged = new Float32Array(this.buf.length + block.length);
      merged.set(this.buf, 0);
      merged.set(block, this.buf.length);
      this.buf = merged;
      while (this.buf.length >= FRAME_SAMPLES) {
        const frame = this.buf.subarray(0, FRAME_SAMPLES);
        this.buf = this.buf.slice(FRAME_SAMPLES);
        shim.levels.downlink = rms(frame);
        const i16 = float32ToInt16(frame);
        if (wsSendBinary(CH_DOWNLINK, i16.buffer)) {
          shim.stats.downlinkFrames++;
          shim.stats.downlinkBytes += i16.byteLength;
        }
      }
    }
  }

  const captureState = { ctx: null, seen: new Set(), node: null, sp: null, encoder: null, mute: null };
  async function attachRemoteTrack(track) {
    if (!track || track.kind !== 'audio' || captureState.seen.has(track.id)) return;
    captureState.seen.add(track.id);
    // 关键兼容：Chromium 中远端轨若没有任何媒体元素拉流，Web Audio 只能采到全 0（已在本地 PC 环回实测确认）。
    // 挂一个音量 0 的 <audio> 强制解码（不外放），采集链才能拿到真实 PCM。
    let puller = null;
    try {
      puller = new Audio();
      puller.srcObject = new MediaStream([track]);
      puller.volume = 0;
      puller.play().catch(() => { /* 自动播放策略由 flags 放开 */ });
    } catch (_) { /* 忽略，采集链仍尝试工作 */ }
    try {
      if (!captureState.ctx) {
        captureState.ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
        await captureState.ctx.audioWorklet.addModule(workletURL());
        try { await captureState.ctx.resume(); } catch (_) { /* flags 已放开自动播放 */ }
      }
      const ctx = captureState.ctx;
      // 关键修复（多轨混音）：会议里每个参会人一条远端音频轨，若每条轨独立
      // encoder 各自向桥接发 PCM，多路 20ms 帧会交错到达——桥接端 adapter 的
      // 有状态 ratecv 在交错流上重采样输出乱码，ASR 只能幻觉。
      // 这里所有轨共用同一个 AudioWorkletNode（Web Audio 自动混音），
      // 只一个 DownlinkEncoder 向桥接发单路混音 PCM。
      if (!captureState.node) {
        captureState.node = new AudioWorkletNode(ctx, 'agent-capture');
        // 关键：经 0 增益 GainNode 挂到 destination，确保图被拉流渲染，又不会自听外放
        captureState.mute = ctx.createGain();
        captureState.mute.gain.value = 0;
        captureState.node.connect(captureState.mute);
        captureState.mute.connect(ctx.destination);
        captureState.encoder = new DownlinkEncoder();
        captureState.node.port.onmessage = (e) => captureState.encoder.push(e.data);
      }
      const src = ctx.createMediaStreamSource(new MediaStream([track]));
      src.connect(captureState.node);   // 多源进同一节点 = 自动混音
      shim.stats.remoteTracks++;
      log('已接管远端音频轨（混音进单路下行）:', track.id, track.label || '(无label)');
      track.addEventListener('ended', () => {
        captureState.seen.delete(track.id);
        shim.stats.remoteTracks = Math.max(0, shim.stats.remoteTracks - 1);
        try { if (puller) { puller.pause(); puller.srcObject = null; } } catch (_) {}
        try { src.disconnect(); } catch (_) {}
      });
    } catch (e) {
      // Blob worklet 在部分环境被拦（如 file:// 来源、页面 CSP 禁 blob:），降级 ScriptProcessor 采集
      warn('AudioWorklet 采集失败，降级 ScriptProcessor:', e);
      try {
        if (!captureState.ctx) captureState.ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
        const ctx = captureState.ctx;
        try { await ctx.resume(); } catch (_) { /* flags 已放开自动播放 */ }
        // 同样做单例混音：所有轨共用同一个 ScriptProcessor
        if (!captureState.sp) {
          captureState.sp = ctx.createScriptProcessor(4096, 1, 1);
          captureState.mute = ctx.createGain();
          captureState.mute.gain.value = 0;
          captureState.sp.connect(captureState.mute);
          captureState.mute.connect(ctx.destination);
          captureState.encoder = new DownlinkEncoder();
          captureState.sp.onaudioprocess = (ev) =>
            captureState.encoder.push(ev.inputBuffer.getChannelData(0).slice(0));
        }
        const src = ctx.createMediaStreamSource(new MediaStream([track]));
        src.connect(captureState.sp);
        shim.stats.remoteTracks++;
        log('已接管远端音频轨（ScriptProcessor 混音）:', track.id, track.label || '(无label)');
        track.addEventListener('ended', () => {
          captureState.seen.delete(track.id);
          shim.stats.remoteTracks = Math.max(0, shim.stats.remoteTracks - 1);
          try { if (puller) { puller.pause(); puller.srcObject = null; } } catch (_) {}
          try { src.disconnect(); } catch (_) {}
        });
      } catch (e2) {
        warn('接管远端轨彻底失败:', e2);
      }
    }
  }

  /* ==================== 8. WebSocket 客户端（断线重连 + 指数退避） ==================== */
  let ws = null;
  let backoffMs = 1000; // 退避：1s -> 2s -> 4s ... 封顶 15s
  function wsConnect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    shim.state = shim.stats.wsReconnects > 0 ? 'reconnecting' : 'connecting';
    try {
      ws = new WebSocket(WS_URL);
      ws.binaryType = 'arraybuffer';
    } catch (e) {
      scheduleReconnect();
      return;
    }
    ws.onopen = () => {
      backoffMs = 1000; // 连上后重置退避
      shim.state = 'connected';
      log('WebSocket 已连接:', WS_URL);
      wsSendControl({ type: 'hello', role: 'page-shim', version: shim.version, sampleRate: SAMPLE_RATE });
    };
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {          // JSON 控制帧
        try { handleControl(JSON.parse(e.data)); } catch (_) { /* 忽略坏消息 */ }
      } else {                                    // 二进制音频帧
        const bytes = new Uint8Array(e.data);
        if (bytes.length < 2) return;
        if (bytes[0] === CH_UPLINK) {
          // 第 1 字节是通道号，后面是 Int16 PCM；slice 拷贝保证 2 字节对齐
          const payload = bytes.slice(1);
          const i16 = new Int16Array(payload.buffer, 0, payload.byteLength >> 1);
          jitter.push(i16);
        }
      }
    };
    ws.onclose = () => {
      ws = null;
      shim.stats.wsReconnects++;
      scheduleReconnect();
    };
    ws.onerror = () => { try { ws && ws.close(); } catch (_) {} };
  }
  function scheduleReconnect() {
    shim.state = 'reconnecting';
    const wait = backoffMs;
    backoffMs = Math.min(backoffMs * 2, 15000); // 指数退避，封顶 15s
    setTimeout(wsConnect, wait);
  }
  function wsSendBinary(channel, payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    const buf = new Uint8Array(1 + payload.byteLength);
    buf[0] = channel;
    buf.set(new Uint8Array(payload), 1);
    try { ws.send(buf); return true; } catch (_) { return false; }
  }
  function wsSendControl(obj) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try { ws.send(JSON.stringify(obj)); } catch (_) {}
  }
  function handleControl(msg) {
    if (msg && msg.type === 'ping') wsSendControl({ type: 'pong', t: Date.now() });
    if (msg && msg.type === 'clear_audio') {
      jitter.reset();           // 真人打断：立刻清空抖动缓冲，智能体马上停声
      log('收到 clear_audio，抖动缓冲已清空');
    }
  }

  /* ==================== 9. mediaDevices 拦截 ==================== */
  // 伪造的设备列表：label 尽量逼真，让页面设备选择 UI 正常显示
  const FAKE_DEVICES = [
    { deviceId: 'default',       kind: 'audioinput',  groupId: 'agent-group-audio', label: '默认 - Microphone (HD Audio Device)' },
    { deviceId: 'communications', kind: 'audioinput', groupId: 'agent-group-audio', label: '通信 - Microphone (HD Audio Device)' },
    { deviceId: 'agent-mic-01',  kind: 'audioinput',  groupId: 'agent-group-audio', label: 'Microphone (HD Audio Device)' },
    { deviceId: 'default',       kind: 'audiooutput', groupId: 'agent-group-spk',   label: '默认 - 扬声器 (Realtek(R) Audio)' },
    { deviceId: 'agent-spk-01',  kind: 'audiooutput', groupId: 'agent-group-spk',   label: '扬声器 (Realtek(R) Audio)' },
    { deviceId: 'agent-cam-01',  kind: 'videoinput',  groupId: 'agent-group-cam',   label: 'Integrated Camera (0408:5099)' },
  ];

  if (navigator.mediaDevices) {
    // 9.1 enumerateDevices：返回伪造列表（label 已就绪，页面无需先授权）
    navigator.mediaDevices.enumerateDevices = async () =>
      FAKE_DEVICES.map((d) => ({
        deviceId: d.deviceId, kind: d.kind, groupId: d.groupId, label: d.label,
        toJSON() { return { deviceId: d.deviceId, kind: d.kind, groupId: d.groupId, label: d.label }; },
      }));

    // 9.2 getUserMedia：音频一律换成合成轨；视频尝试走原始实现，失败则丢弃视频
    const origGetUserMedia = navigator.mediaDevices.getUserMedia
      ? navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
      : null;
    navigator.mediaDevices.getUserMedia = async (constraints = {}) => {
      const wantsAudio = !!constraints.audio;
      const wantsVideo = !!constraints.video;
      const stream = new MediaStream();
      if (wantsVideo && origGetUserMedia) {
        try {
          const vs = await origGetUserMedia({ video: constraints.video, audio: false });
          vs.getVideoTracks().forEach((t) => stream.addTrack(t));
        } catch (e) {
          warn('视频采集失败，丢弃视频轨（音频不受影响）:', e && e.message);
        }
      }
      if (wantsAudio) {
        // 无论请求什么 deviceId/约束，都返回同一条合成轨
        stream.addTrack(await getSyntheticTrack());
      }
      if (!stream.getTracks().length) {
        throw new DOMException('Requested device not found', 'NotFoundError');
      }
      return stream;
    };
    log('mediaDevices 拦截完成（getUserMedia / enumerateDevices）');
  } else {
    warn('navigator.mediaDevices 不存在，注入环境异常');
  }

  /* ==================== 10. RTCPeerConnection 包装 ==================== */
  const OrigPC = window.RTCPeerConnection || window.webkitRTCPeerConnection;
  if (OrigPC) {
    // class extends 原生类：自动保留原型链，静态方法也随原型继承
    class AgentRTCPeerConnection extends OrigPC {
      constructor(config, constraints) {
        super(config, constraints);
        // 内部先挂一个 track 监听，不影响业务侧自己的 ontrack/addEventListener
        this.addEventListener('track', (ev) => {
          if (ev.track && ev.track.kind === 'audio') attachRemoteTrack(ev.track);
        });
      }
    }
    // 把原生自有静态属性/方法逐一拷贝过来，行为与原生保持一致
    for (const name of Object.getOwnPropertyNames(OrigPC)) {
      if (['length', 'name', 'prototype'].includes(name)) continue;
      try {
        Object.defineProperty(AgentRTCPeerConnection, name, Object.getOwnPropertyDescriptor(OrigPC, name));
      } catch (_) { /* 只读属性跳过 */ }
    }
    window.RTCPeerConnection = AgentRTCPeerConnection;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = AgentRTCPeerConnection;
    log('RTCPeerConnection 包装完成（原型链与静态方法已保留）');
  } else {
    warn('未找到 RTCPeerConnection，远端音频捕获不可用');
  }

  /* ==================== 11. 启动 ==================== */
  wsConnect();               // 立即连接后端（未连上时下行帧丢弃，由重连兜底）
  getSyntheticTrack();       // 预创建合成轨，避免页面第一次 getUserMedia 时等待
  shim.sendControl = wsSendControl;  // 开放给热注入脚本：window.__agentShim.sendControl({...})

  /* ==================== 12. 会议聊天文字采集（v3 精确锚点 + 自动展开面板） ==================== */
  // 实测飞书会议聊天 DOM（dom_probe 挖出的真实结构）：
  //   消息块 [data-position]；发送人 span.eBhDBaXQ；时间 span.hmR3HfKk；正文 span.pJ07o4qa
  //   聊天按钮 button:has([data-icon^="Chat"])，Filled=面板已开 / Outlined=未开
  const chatSeen = new Set();

  function emitChat(sender, text, time) {
    const name = (sender || '会议聊天').trim() || '会议聊天';
    const t = (text || '').replace(/\s+/g, ' ').trim();
    if (t.length < 1 || t.length > 300) return;
    let h = 0;
    // 去重键含消息时间：同一人不同时间发相同文本不算重复（测试场景高频）
    const key = name + '|' + (time || '') + '|' + t;
    for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
    if (chatSeen.has(h)) return;
    chatSeen.add(h);
    wsSendControl({ type: 'chat.message', speaker: name, text: t, ts: Date.now() });
    shim.stats.chatMessages = (shim.stats.chatMessages || 0) + 1;
    log('聊天消息上报: [%s] %s', name, t.slice(0, 50));
  }

  function scanChat() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    for (const el of document.querySelectorAll('span.pJ07o4qa')) {
      if (!(el instanceof HTMLElement) || el.offsetParent === null) continue;
      const box = el.closest('[data-position]') || el.parentElement;
      const nameEl = box.querySelector('span.eBhDBaXQ');
      const timeEl = box.querySelector('span.hmR3HfKk');
      emitChat(nameEl ? nameEl.innerText : '会议聊天', el.innerText,
               timeEl ? timeEl.innerText : '');
    }
  }

  function chatPanelOpen() {
    return !!document.querySelector('span.pJ07o4qa')
      || !!document.querySelector('button [data-icon="ChatFilled"]')
      || [...document.querySelectorAll('textarea,input')].some((e) => (e.placeholder || '').includes('发送消息'));
  }

  function ensureChatPanel() {
    if (chatPanelOpen()) return;
    const btn = [...document.querySelectorAll('button')]
      .find((b) => b.querySelector('[data-icon="ChatOutlined"]') && b.offsetParent !== null);
    if (btn) { btn.click(); log('已自动展开聊天面板'); }
  }
  setInterval(ensureChatPanel, 8000);   // 每 8s 自检，面板未开则自动点开（幂等）

  /* ==================== 13. 投屏/视频画面采集（最大 video 截帧 → screen.frame） ==================== */
  const screenState = { lastHash: 0 };
  function grabScreenFrame() {
    const videos = [...document.querySelectorAll('video')]
      .filter((v) => v.videoWidth > 0 && v.readyState >= 2);
    if (!videos.length) return null;
    videos.sort((a, b) => b.videoWidth * b.videoHeight - a.videoWidth * a.videoHeight);
    const v = videos[0];
    const w = Math.min(v.videoWidth, 960);                 // 限宽 960，控制单帧体积
    const h = Math.round(v.videoHeight * w / v.videoWidth);
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    c.getContext('2d').drawImage(v, 0, 0, w, h);
    return (c.toDataURL('image/jpeg', 0.7).split(',')[1]) || null;
  }
  function scanScreen() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    let b64 = null;
    try { b64 = grabScreenFrame(); } catch (_) { return; }  // canvas 异常直接跳过本轮
    if (!b64) return;
    let h = 0;                                              // 抽样哈希：画面没变化不重发
    for (let i = 0; i < b64.length; i += 997) h = (h * 31 + b64.charCodeAt(i)) >>> 0;
    if (h === screenState.lastHash) return;
    screenState.lastHash = h;
    wsSendControl({ type: 'screen.frame', image_b64: b64, ts: Date.now() });
    shim.stats.screenFrames = (shim.stats.screenFrames || 0) + 1;
    log('投屏帧上报:', Math.round(b64.length / 1024) + 'KB');
  }

  setInterval(scanChat, 3000);      // 聊天 3s 一扫
  setInterval(scanScreen, 5000);    // 投屏 5s 一帧（仅变化时发送）
  log('注入完成，等待页面调用 getUserMedia / 建立 RTC 连接');
})();
