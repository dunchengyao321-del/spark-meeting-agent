/* features_chat_reply_v2.js —— 热注入特性 v2：代发答复到会议聊天框
 * 修复：飞书聊天输入框是 <pre class="lark-editor" contenteditable>，
 * 必须用 execCommand('insertText') 走编辑器输入管线，再点"发送"按钮（兜底 Enter）。
 */
(() => {
  'use strict';
  if (window.__chatReplyV2Injected) return 'already-injected-v2';
  Object.defineProperty(window, '__chatReplyV2Injected', { value: true, configurable: true });

  const WS_URL = (window.__AGENT_WS_URL) || 'ws://127.0.0.1:8876/ws';
  let ws = null;

  function findEditor() {
    return document.querySelector('pre.lark-editor[contenteditable="true"]')
      || document.querySelector('[contenteditable="true"].lark-editor')
      || document.querySelector('[contenteditable="true"]');
  }

  function clickSend() {
    const btn = [...document.querySelectorAll('button, [role="button"], span, div, a')]
      .find((b) => {
        const t = (b.innerText || '').trim();
        return (t === '发送' || t === 'Send') && b.offsetParent !== null && b.children.length <= 2;
      });
    if (btn) { btn.click(); return true; }
    return false;
  }

  function postToChat(text) {
    const editor = findEditor();
    if (!editor || !text) { console.warn('[ChatReplyV2] 找不到编辑器'); return; }
    editor.focus();
    const ok = document.execCommand('insertText', false, text);
    if (!ok) {
      // execCommand 被禁用的极端情况：直接写 innerText + 手工 input 事件
      editor.innerText = text;
      editor.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
    }
    setTimeout(() => {
      if (!clickSend()) {
        editor.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
        console.log('[ChatReplyV2] 未找到发送按钮，已派发 Enter');
      } else {
        console.log('[ChatReplyV2] 已发送:', text.slice(0, 40));
      }
    }, 400);
  }

  function connect() {
    ws = new WebSocket(WS_URL);
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'hello', role: 'page-features', feature: 'chat-reply-v2' }));
      console.log('[ChatReplyV2] 已连接桥接');
    };
    ws.onmessage = (e) => {
      if (typeof e.data !== 'string') return;
      let msg = null;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (msg && msg.type === 'chat.post' && msg.text) postToChat(msg.text);
    };
    ws.onclose = () => setTimeout(connect, 5000);
    ws.onerror = () => { try { ws.close(); } catch (_) {} };
  }
  connect();
  return 'chat-reply-v2 injected';
})();
