/* minutes_collector.js —— 妙记网页版实时转写采集（热注入/特性脚本双用）
 *
 * 在 duodian.feishu.cn/minutes/<token> 详情页运行：
 *   1) 每 2.5s 扫描全部 .record-paragraph（说话人 .record-paragraph-user-name，
 *      文本 .record-paragraph-text），拼成全量段落数组；
 *   2) 内容哈希有变化才 POST 到 B 智能体 http://127.0.0.1:8766/minutes_ingest
 *      （段落会随转写修正变化，B 端全量覆盖写 minutes_live.txt）；
 *   3) 状态暴露 window.__minutesCollector 供探针读取。
 *
 * 注意：https 页面 fetch http://127.0.0.1 依赖 Chrome 对 localhost 的混合内容豁免，
 * 与 shim 的 ws://127.0.0.1 同理，已验证可行。
 */
(() => {
  if (window.__minutesCollectorInjected) return;
  Object.defineProperty(window, '__minutesCollectorInjected', { value: true });

  const B_URL = 'http://127.0.0.1:8766/minutes_ingest';
  const SCAN_INTERVAL = 2500;
  const state = { scans: 0, posts: 0, paragraphs: 0, lastPostAt: 0, lastError: '', running: false };
  Object.defineProperty(window, '__minutesCollector', { value: state, configurable: true });

  let lastHash = '';

  function collect() {
    const paras = [];
    document.querySelectorAll('.paragraphs-container .record-paragraph').forEach(p => {
      const speaker = (p.querySelector('.record-paragraph-user-name')?.innerText || '').trim() || '?';
      const text = (p.querySelector('.record-paragraph-text')?.innerText || '').replace(/\s+/g, ' ').trim();
      if (text) paras.push({ speaker, text });
    });
    return paras;
  }

  function hashOf(paras) {
    let h = 0;
    for (const p of paras) {
      const s = p.speaker + '|' + p.text;
      for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    }
    return h + ':' + paras.length;
  }

  async function push(paras) {
    try {
      const resp = await fetch(B_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paragraphs: paras, source: 'minutes-web' }),
      });
      const d = await resp.json();
      state.posts++;
      state.lastPostAt = Date.now();
      state.lastError = d.ok ? '' : (d.error || 'unknown');
    } catch (e) {
      state.lastError = String(e).slice(0, 120);
    }
  }

  async function tick() {
    if (!location.href.includes('/minutes/')) return;  // 离开妙记页则静默
    state.scans++;
    const paras = collect();
    state.paragraphs = paras.length;
    const h = hashOf(paras);
    if (h === lastHash || !paras.length) return;
    lastHash = h;
    await push(paras);
  }

  state.running = true;
  setInterval(tick, SCAN_INTERVAL);
  tick();
  console.log('[minutes_collector] 妙记实时转写采集已启动 ->', B_URL);
})();
