// Spark meeting console — zero-build ES module client.
// Talks to the local server via one WebSocket: JSON events + raw PCM audio.

const $ = (id) => document.getElementById(id);

const els = {
  start: $('start'), stop: $('stop'), conn: $('conn'),
  transcripts: $('transcripts'), shareMeeting: $('share_meeting'),
  meetingState: $('meeting_state'),
  injectText: $('inject_text'), injectSay: $('inject_say'), forceNext: $('force_next'),
  syslog: $('syslog'),
  agentState: $('agent_state'), agentStateText: $('agent_state_text'), triggers: $('triggers'),
  metricsBody: $('metrics_body'),
  kbStats: $('kb_stats'), kbQuery: $('kb_query'), kbSearch: $('kb_search'),
  kbIngest: $('kb_ingest'), kbHits: $('kb_hits'), prefetch: $('prefetch'),
  mcpStats: $('mcp_stats'), mcpStart: $('mcp_start'),
  settingsModal: $('settings_modal'), settingsOpen: $('settings_open'),
  settingsClose: $('settings_close'), settingsSave: $('settings_save'),
  settingsStatus: $('settings_status'),
};

const state = {
  ws: null, audioCtx: null, playbackNode: null,
  micStream: null, micNode: null, meetingStream: null, meetingNode: null,
  running: false, pendingAgent: null, partialUsers: {}, turnCount: 0,
};

function wsUrl(path) {
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${scheme}//${location.host}${path}`;
}

async function ensureAudioContext() {
  state.audioCtx = state.audioCtx || new AudioContext();
  await state.audioCtx.resume();
  await ensureWorklets();
}

// ------------------------------------------------------------------ audio
async function ensureWorklets() {
  await state.audioCtx.audioWorklet.addModule('capture-worklet.js');
  await state.audioCtx.audioWorklet.addModule('playback-worklet.js');
}

function attachCapture(stream, channel) {
  const source = state.audioCtx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(state.audioCtx, 'capture-processor',
    { processorOptions: { targetRate: 16000 } });
  node.port.onmessage = (event) => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN || !state.running) return;
    const floats = event.data;
    const frame = new Uint8Array(1 + floats.length * 2);
    frame[0] = channel;
    const view = new DataView(frame.buffer);
    for (let i = 0; i < floats.length; i++) {
      const s = Math.max(-1, Math.min(1, floats[i]));
      view.setInt16(1 + i * 2, s * 32767, true);
    }
    state.ws.send(frame.buffer);
  };
  source.connect(node);
  // Do not connect to destination: capture only.
  return { source, node };
}

function floatToPlayback(int16ArrayBuffer) {
  if (!state.playbackNode) return;
  const pcm = new Int16Array(int16ArrayBuffer);
  const floats = new Float32Array(pcm.length);
  for (let i = 0; i < pcm.length; i++) floats[i] = pcm[i] / 32768;
  state.playbackNode.port.postMessage(floats, [floats.buffer]);
}

// ---------------------------------------------------------------- session
async function startSession() {
  try {
    await ensureAudioContext();
    state.playbackNode = new AudioWorkletNode(state.audioCtx, 'playback-processor');
    state.playbackNode.connect(state.audioCtx.destination);

    state.micStream = await navigator.mediaDevices.getUserMedia({
      // autoGainControl 必须关：TTS 经扬声器外放时浏览器 AGC 会逐轮压低
      // 麦克风增益，几轮对话后人声被压到 ASR 无法识别（实测峰值衰减到 ~1/6）。
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false, channelCount: 1 },
    });
    const mic = attachCapture(state.micStream, 0);
    state.micNode = mic;

    const ws = new WebSocket(wsUrl('/ws/meeting'));
    ws.binaryType = 'arraybuffer';
    state.ws = ws;
    ws.onopen = () => setConn('on', '已连接');
    ws.onclose = () => {
      setConn('idle', '未连接');
      if (state.running) stopSession(true);
    };
    ws.onerror = () => logLine('err', 'WebSocket 连接失败：请确认服务已启动');
    ws.onmessage = (event) => {
      if (typeof event.data === 'string') handleEvent(JSON.parse(event.data));
      else floatToPlayback(event.data);
    };

    state.running = true;
    els.start.disabled = true;
    els.stop.disabled = false;
    clearTranscripts();
  } catch (exc) {
    logLine('err', `启动失败：${exc.message || exc}`);
    stopSession(true);
  }
}

function stopSession(silent) {
  state.running = false;
  if (state.ws) {
    try {
      if (state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: 'session.stop' }));
        state.ws.close();
      }
    } catch (_) { /* ignore */ }
    state.ws = null;
  }
  if (state.micNode) {
    try { state.micNode.source.disconnect(); } catch (_) {}
    try { state.micNode.node.disconnect(); } catch (_) {}
    state.micNode = null;
  }
  if (state.meetingNode) {
    try { state.meetingNode.source.disconnect(); } catch (_) {}
    try { state.meetingNode.node.disconnect(); } catch (_) {}
    state.meetingNode = null;
  }
  for (const track of (state.micStream?.getTracks() || [])) track.stop();
  for (const track of (state.meetingStream?.getTracks() || [])) track.stop();
  state.micStream = null;
  state.meetingStream = null;
  if (state.playbackNode) { try { state.playbackNode.disconnect(); } catch (_) {} state.playbackNode = null; }
  setConn('idle', '未连接');
  setMeetingPill(false);
  setAgentState('idle', '待命');
  els.start.disabled = false;
  els.stop.disabled = true;
  if (!silent) logLine('ok', '会话已停止');
}

async function shareMeeting() {
  try {
    await ensureAudioContext();
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: { echoCancellation: false, noiseSuppression: false },
    });
    stream.getVideoTracks().forEach((t) => t.stop());
    if (!stream.getAudioTracks().length) {
      logLine('warn', '未获得音频：共享时请勾选「分享音频」（建议选标签页）');
      return;
    }
    state.meetingStream = stream;
    state.meetingNode = attachCapture(stream, 1);
    stream.getAudioTracks()[0].onended = () => setMeetingPill(false);
    setMeetingPill(true);
    logLine('ok', '会议声音已接入（声道：会议）');
  } catch (exc) {
    logLine('warn', `会议声音接入取消或失败：${exc.message || exc}`);
  }
}

function setMeetingPill(on) {
  els.meetingState.textContent = on ? '会议声音：已接入' : '会议声音：未接入';
  els.meetingState.className = `pill ${on ? 'on' : 'idle'}`;
}

// ---------------------------------------------------------------- events
const SPEAKER_CLASS = { '我': 'user', '星火': 'agent', agent: 'agent', console: 'agent' };

function clearTranscripts() {
  els.transcripts.innerHTML = '';
}

function appendBubble(speaker, text, pending) {
  const empty = els.transcripts.querySelector('.empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  const cls = SPEAKER_CLASS[speaker] || (speaker === 'agent' ? 'agent' : 'other');
  div.className = `bubble ${cls}${pending ? ' pending' : ''}`;
  const who = document.createElement('span');
  who.className = 'who';
  who.textContent = speaker === 'agent' ? '星火（智能体）' : speaker;
  div.appendChild(who);
  const body = document.createElement('span');
  body.textContent = text;
  div.appendChild(body);
  els.transcripts.appendChild(div);
  els.transcripts.scrollTop = els.transcripts.scrollHeight;
  return div;
}

function handleEvent(event) {
  switch (event.type) {
    case 'session.state':
      if (event.status === 'connected') {
        setConn('on', `已连接 · ${event.engine}`);
        logLine('ok', `引擎 ${event.engine} 已连接${event.llm_model ? ` · LLM ${event.llm_model}` : ''}`);
      } else if (event.status === 'connecting') {
        setConn('warn', '连接中…');
      } else if (event.status === 'stopped') {
        setConn('idle', '已停止');
      }
      if (event.triggers) els.triggers.textContent = '触发方式：' + event.triggers.join('；');
      break;
    case 'agent.state': {
      const label = { listening: '聆听中', thinking: '思考中', speaking: '发言中' }[event.state] || event.state;
      setAgentState(event.state, event.reason ? `${label}（${event.reason}）` : label);
      if (event.state === 'listening' && state.pendingAgent) {
        state.pendingAgent.classList.remove('pending');
        state.pendingAgent = null;
      }
      break;
    }
    case 'transcript.partial':
      if (event.speaker === 'agent') {
        if (!state.pendingAgent) state.pendingAgent = appendBubble('agent', '', true);
        state.pendingAgent.lastChild.textContent += event.text;
        els.transcripts.scrollTop = els.transcripts.scrollHeight;
      } else if (event.partial_asr) {
        // Interim ASR snapshot: one replaceable draft bubble per speaker.
        const sp = event.speaker;
        let bubble = state.partialUsers[sp];
        if (!bubble) {
          bubble = appendBubble(sp, '', true);
          state.partialUsers[sp] = bubble;
        }
        bubble.lastChild.textContent = event.text;
        els.transcripts.scrollTop = els.transcripts.scrollHeight;
      }
      break;
    case 'transcript.final':
      if (state.partialUsers[event.speaker]) {
        const bubble = state.partialUsers[event.speaker];
        delete state.partialUsers[event.speaker];
        if (event.text) {
          bubble.lastChild.textContent = event.text;
          bubble.classList.remove('pending');
        } else {
          bubble.remove();
        }
      } else if (event.speaker === 'agent' && state.pendingAgent) {
        state.pendingAgent.lastChild.textContent = event.text;
        state.pendingAgent.classList.remove('pending');
        state.pendingAgent = null;
      } else if (event.text) {
        appendBubble(event.speaker, event.text);
      }
      break;
    case 'clear_audio':
      if (state.playbackNode) state.playbackNode.port.postMessage('clear');
      break;
    case 'metrics.turn': {
      state.turnCount += 1;
      const row = document.createElement('tr');
      const fmt = (v) => (v === undefined || v === null ? '–' : `${v}ms`);
      row.innerHTML = `<td>#${state.turnCount}</td><td>${fmt(event.asr_ms)}</td>`
        + `<td>${fmt(event.retrieval_ms)}</td><td>${fmt(event.llm_ttft_ms)}</td>`
        + `<td>${fmt(event.tts_ttfa_ms)}</td><td>${fmt(event.total_ms ?? event.voice_to_voice_ms)}</td>`;
      els.metricsBody.prepend(row);
      while (els.metricsBody.children.length > 8) els.metricsBody.lastChild.remove();
      logLine('ok', `本轮响应 ${(event.total_ms ?? event.voice_to_voice_ms)}ms`);
      break;
    }
    case 'kb.prefetch':
      prefetchLine(`预热：「${event.query}」→ ${event.hits} 条${event.warm ? '（命中缓存）' : ''}`, event.warm);
      break;
    case 'mcp.call':
      logLine('warn', `调用工具 ${event.name}`);
      break;
    case 'mcp.result':
      logLine('ok', `工具返回：${String(event.output).slice(0, 80)}`);
      break;
    case 'session.error':
      logLine('err', event.error);
      break;
  }
}

function setConn(cls, text) {
  els.conn.className = `pill ${cls}`;
  els.conn.textContent = text;
}

function setAgentState(cls, text) {
  els.agentState.className = `agent-state ${cls === 'idle' ? 'listening' : cls}`;
  els.agentStateText.textContent = text;
}

function logLine(cls, text) {
  const div = document.createElement('div');
  div.className = cls;
  div.textContent = `${new Date().toLocaleTimeString()} ${text}`;
  els.syslog.prepend(div);
  while (els.syslog.children.length > 60) els.syslog.lastChild.remove();
}

function prefetchLine(text, warm) {
  const div = document.createElement('div');
  if (warm) div.className = 'warm';
  div.textContent = `${new Date().toLocaleTimeString()} ${text}`;
  els.prefetch.prepend(div);
  while (els.prefetch.children.length > 20) els.prefetch.lastChild.remove();
}

// ------------------------------------------------------------- status / kb
async function refreshStatus() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    els.kbStats.textContent = `目录 ${data.kb.dir} · ${data.kb.chunks} 个片段`
      + (data.kb.warm_entries ? ` · 预热缓存 ${data.kb.warm_entries}` : '');
    const sessionText = data.session.active
      ? `会话：${data.session.engine} 运行中`
      : `会话：${data.engine_default} 待命`;
    logLineOnce('ok', sessionText);
    if (!data.mcp.length) {
      els.mcpStats.textContent = '未配置 MCP 服务器（config.json → mcp_servers）';
    } else {
      els.mcpStats.innerHTML = data.mcp.map((s) =>
        `${s.name}：${s.status}${s.status === 'connected' ? `（${s.tools.length} 个工具）` : ''}${s.error ? ` · ${s.error}` : ''}`).join('<br>');
    }
    if (!data.keys.llm) logLineOnce('warn', '尚未配置 API Key：打开「设置」填写', 'key');
  } catch (_) { /* server offline */ }
}
const onceFlags = { session: false, key: false };
function logLineOnce(cls, text, flag = 'session') {
  if (onceFlags[flag]) return;
  onceFlags[flag] = true;
  logLine(cls, text);
}

async function searchKb() {
  const q = els.kbQuery.value.trim();
  if (!q) return;
  const res = await fetch(`/api/kb/search?q=${encodeURIComponent(q)}&k=4`);
  const data = await res.json();
  els.kbHits.innerHTML = '';
  if (!data.hits.length) {
    els.kbHits.innerHTML = '<div class="kb-hit">没有命中。把资料放进 docs/kb/ 后点「刷新」。</div>';
    return;
  }
  for (const hit of data.hits) {
    const div = document.createElement('div');
    div.className = 'kb-hit';
    div.innerHTML = `<div class="src">${hit.source}${hit.heading ? ' · ' + hit.heading : ''} · ${hit.score}</div>`;
    const body = document.createElement('div');
    body.textContent = hit.text.slice(0, 180);
    div.appendChild(body);
    els.kbHits.appendChild(div);
  }
}

// ---------------------------------------------------------------- config
const FIELD_MAP = {
  f_llm_provider: 'llm_provider',
  f_llm_base_url: 'llm_base_url',
  f_llm_model: 'llm_model',
  f_volcano_llm_model: 'volcano_llm_model',
  f_volcano_llm_base_url: 'volcano_llm_base_url',
  f_asr_engine: 'asr_engine',
  f_tts_engine: 'tts_engine',
  f_tts_voice: 'tts_voice',
  f_volcano_tts_api_key: 'volcano_tts_api_key',
  f_volcano_tts_speaker: 'volcano_tts_speaker',
  f_apple_asr_locale: 'apple_asr_locale',
  f_kb_dir: 'kb_dir',
  f_meeting_silence_ms: 'meeting_silence_ms',
  f_volcano_api_key: 'volcano_api_key',
  f_meeting_partial_asr: 'meeting_partial_asr',
  f_meeting_answer_questions: 'meeting_answer_questions',
  f_meeting_tool_filler: 'meeting_tool_filler',
};

async function openSettings() {
  const res = await fetch('/api/config');
  const cfg = await res.json();
  for (const [id, key] of Object.entries(FIELD_MAP)) {
    const el = $(id);
    if (!el) continue;
    if (el.type === 'checkbox') {
      const v = cfg[key];
      el.checked = !(v === false || v === 0 || v === 'off' || v === 'false');
      continue;
    }
    if (el.tagName === 'SELECT') {
      const val = cfg[key] ?? '';
      if ([...el.options].some(o => o.value === val)) el.value = val;
    } else if (el.tagName === 'TEXTAREA') {
      el.value = cfg[key] ?? '';
    } else {
      el.value = cfg[key] ?? '';
    }
  }
  $('f_meeting_wake_names').value = Array.isArray(cfg.meeting_wake_names)
    ? cfg.meeting_wake_names.join('，') : (cfg.meeting_wake_names || '');
  $('f_volcano_api_key').value = '';
  els.settingsStatus.textContent = cfg.key_configured ? 'API Key 已配置（留空保存保持不变）' : '尚未配置 API Key';
  els.settingsModal.hidden = false;
}

async function saveSettings() {
  const payload = {};
  for (const [id, key] of Object.entries(FIELD_MAP)) {
    const el = $(id);
    if (!el) continue;
    if (el.type === 'checkbox') {
      payload[key] = el.checked;
      continue;
    }
    const val = el.tagName === 'TEXTAREA' ? el.value : el.value.trim();
    if (val) payload[key] = val;
  }
  payload.meeting_wake_names = $('f_meeting_wake_names').value
    .split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  payload.meeting_silence_ms = parseInt($('f_meeting_silence_ms').value, 10) || 700;
  const volcanoKey = $('f_volcano_api_key').value.trim();
  if (volcanoKey) payload.volcano_api_key = volcanoKey;
  const res = await fetch('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (data.ok) {
    els.settingsStatus.textContent = '已保存';
  } else {
    els.settingsStatus.textContent = '保存失败：' + (data.error || '');
  }
  refreshStatus();
}

// ------------------------------------------------------------------ init
els.start.addEventListener('click', startSession);
els.stop.addEventListener('click', () => stopSession(false));
els.shareMeeting.addEventListener('click', shareMeeting);
els.injectSay.addEventListener('click', () => {
  const text = els.injectText.value.trim();
  if (!text || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
  state.ws.send(JSON.stringify({ type: 'inject.say', text }));
  appendBubble('console', `（指令）${text}`);
  els.injectText.value = '';
});
els.injectText.addEventListener('keydown', (e) => { if (e.key === 'Enter') els.injectSay.click(); });
els.forceNext.addEventListener('click', () => {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: 'agent.force' }));
    logLine('ok', '已举手：下一位说完后星火接话');
  }
});
els.kbSearch.addEventListener('click', searchKb);
els.kbQuery.addEventListener('keydown', (e) => { if (e.key === 'Enter') searchKb(); });
els.kbIngest.addEventListener('click', async () => {
  const res = await fetch('/api/kb/ingest', { method: 'POST' });
  const data = await res.json();
  logLine('ok', `知识库已刷新：${data.files} 个文件 / ${data.chunks} 个片段`);
  refreshStatus();
});
els.mcpStart.addEventListener('click', async () => {
  const res = await fetch('/api/mcp/start', { method: 'POST' });
  await res.json();
  refreshStatus();
});
els.settingsOpen.addEventListener('click', openSettings);
$('apple_auth').addEventListener('click', async () => {
  $('apple_auth_status').textContent = '正在请求授权…若桌面弹出对话框请点「允许」';
  try {
    const res = await fetch('/api/asr/apple_auth', { method: 'POST' });
    const data = await res.json();
    $('apple_auth_status').textContent = data.authorized
      ? '已授权，本地语音识别可用'
      : (data.detail === 'prompt_shown'
        ? '授权窗口已弹出，请在桌面点「允许」，然后再点一次本按钮确认'
        : `未授权（${data.detail || data.error || 'rc=' + data.rc}）`);
  } catch (e) {
    $('apple_auth_status').textContent = '授权请求失败：' + e;
  }
});
function closeSettings() { els.settingsModal.hidden = true; }
els.settingsClose.addEventListener('click', closeSettings);
els.settingsModal.addEventListener('click', (e) => { if (e.target === els.settingsModal) closeSettings(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !els.settingsModal.hidden) closeSettings(); });
els.settingsSave.addEventListener('click', saveSettings);

refreshStatus();
setInterval(refreshStatus, 8000);
