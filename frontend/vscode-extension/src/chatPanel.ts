import * as vscode from 'vscode';
import * as http from 'http';

export class ChatPanel {
  public static currentPanel: ChatPanel | undefined;
  private readonly _panel: vscode.WebviewPanel;
  private readonly _extensionUri: vscode.Uri;
  private readonly _context: vscode.ExtensionContext;
  private _disposables: vscode.Disposable[] = [];
  private _backendUrl = 'http://localhost:8000';

  public static createOrShow(extensionUri: vscode.Uri, context: vscode.ExtensionContext) {
    const column = vscode.window.activeTextEditor
      ? vscode.window.activeTextEditor.viewColumn
      : undefined;

    if (ChatPanel.currentPanel) {
      ChatPanel.currentPanel._panel.reveal(column);
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      'awscheppuChat',
      'AWS Cheppu',
      column || vscode.ViewColumn.Beside,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [extensionUri]
      }
    );

    ChatPanel.currentPanel = new ChatPanel(panel, extensionUri, context);
  }

  private constructor(panel: vscode.WebviewPanel, extensionUri: vscode.Uri, context: vscode.ExtensionContext) {
    this._panel = panel;
    this._extensionUri = extensionUri;
    this._context = context;

    this._panel.webview.html = this._getHtmlContent();
    this._panel.onDidDispose(() => this.dispose(), null, this._disposables);

    this._panel.webview.onDidReceiveMessage(async (message) => {
      switch (message.command) {
        case 'sendMessage':
          await this._handleUserMessage(message.text);
          break;
        case 'confirm':
          await this._handleConfirmation(message.sessionId, message.confirmed);
          break;
      }
    }, null, this._disposables);
  }

  private async _handleUserMessage(text: string) {
    this._panel.webview.postMessage({ command: 'addUserMessage', text });
    this._panel.webview.postMessage({ command: 'setLoading', loading: true });

    try {
      const response = await this._callBackend('/chat', { message: text });
      this._panel.webview.postMessage({ command: 'setLoading', loading: false });

      if (response.requires_confirmation) {
        this._panel.webview.postMessage({
          command: 'addConfirmationMessage',
          text: response.message,
          sessionId: response.session_id,
          risk: response.risk_level
        });
      } else {
        this._panel.webview.postMessage({ command: 'addAiMessage', text: response.message });
      }

      if (response.audio_url) {
        this._panel.webview.postMessage({ command: 'playAudio', url: `${this._backendUrl}${response.audio_url}` });
      }
    } catch (err: any) {
      this._panel.webview.postMessage({ command: 'setLoading', loading: false });
      this._panel.webview.postMessage({
        command: 'addAiMessage',
        text: `⚠️ Error connecting to backend: ${err.message}. Make sure the backend is running on port 8000.`
      });
    }
  }

  private async _handleConfirmation(sessionId: string, confirmed: boolean) {
    this._panel.webview.postMessage({ command: 'setLoading', loading: true });
    try {
      const response = await this._callBackend('/confirm', { session_id: sessionId, confirmed });
      this._panel.webview.postMessage({ command: 'setLoading', loading: false });
      this._panel.webview.postMessage({ command: 'addAiMessage', text: response.message });
      if (response.audio_url) {
        this._panel.webview.postMessage({ command: 'playAudio', url: `${this._backendUrl}${response.audio_url}` });
      }
    } catch (err: any) {
      this._panel.webview.postMessage({ command: 'setLoading', loading: false });
      this._panel.webview.postMessage({ command: 'addAiMessage', text: `⚠️ Error: ${err.message}` });
    }
  }

  private _callBackend(path: string, body: object): Promise<any> {
    return new Promise((resolve, reject) => {
      const data = JSON.stringify(body);
      const options = {
        hostname: 'localhost',
        port: 8000,
        path,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(data)
        }
      };

      const req = http.request(options, (res) => {
        let responseData = '';
        res.on('data', (chunk) => { responseData += chunk; });
        res.on('end', () => {
          try {
            resolve(JSON.parse(responseData));
          } catch {
            reject(new Error('Invalid JSON response from backend'));
          }
        });
      });

      req.on('error', reject);
      req.setTimeout(30000, () => { req.destroy(); reject(new Error('Request timed out')); });
      req.write(data);
      req.end();
    });
  }

  private _getHtmlContent(): string {
    return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AWS Cheppu</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', monospace;
    background: #0d0f14;
    color: #c9d1d9;
    height: 100vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* Header */
  .header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px;
    background: #161b22;
    border-bottom: 1px solid #21262d;
  }
  .header-logo {
    width: 28px; height: 28px;
    background: linear-gradient(135deg, #f78166, #ff6b35);
    border-radius: 6px;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px;
  }
  .header-title {
    font-size: 13px;
    font-weight: 600;
    color: #e6edf3;
    letter-spacing: 0.05em;
  }
  .header-subtitle {
    font-size: 10px;
    color: #6e7681;
    margin-left: auto;
  }
  .status-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: #3fb950;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  /* Messages */
  .messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    scrollbar-width: thin;
    scrollbar-color: #30363d transparent;
  }
  .messages::-webkit-scrollbar { width: 4px; }
  .messages::-webkit-scrollbar-thumb { background: #30363d; border-radius: 2px; }

  .message {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-width: 100%;
    animation: fadeIn 0.2s ease;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .message-label {
    font-size: 9px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    font-weight: 600;
  }
  .message.user .message-label  { color: #58a6ff; }
  .message.ai    .message-label  { color: #f78166; }
  .message.system .message-label { color: #6e7681; }

  .message-bubble {
    padding: 10px 12px;
    border-radius: 6px;
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .message.user .message-bubble {
    background: #1c2128;
    border: 1px solid #30363d;
    color: #c9d1d9;
  }
  .message.ai .message-bubble {
    background: #161b22;
    border: 1px solid #21262d;
    color: #c9d1d9;
  }

  /* Risk badges */
  .risk-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .risk-HIGH   { background: #3d1a1a; color: #f85149; border: 1px solid #f85149; }
  .risk-MEDIUM { background: #2d2000; color: #d29922; border: 1px solid #d29922; }
  .risk-LOW    { background: #0d2210; color: #3fb950; border: 1px solid #3fb950; }

  /* Confirmation card */
  .confirmation-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 12px;
  }
  .confirmation-text {
    font-size: 12px;
    line-height: 1.6;
    color: #c9d1d9;
    margin-bottom: 10px;
    white-space: pre-wrap;
  }
  .confirm-buttons {
    display: flex;
    gap: 8px;
  }
  .btn-confirm {
    padding: 6px 14px;
    border-radius: 4px;
    border: none;
    cursor: pointer;
    font-size: 11px;
    font-family: inherit;
    font-weight: 600;
    transition: opacity 0.15s;
  }
  .btn-confirm:hover { opacity: 0.8; }
  .btn-yes { background: #238636; color: #fff; }
  .btn-no  { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; }

  /* Loading */
  .loading-msg {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 0;
    font-size: 11px;
    color: #6e7681;
  }
  .loading-dots span {
    display: inline-block;
    width: 4px; height: 4px;
    border-radius: 50%;
    background: #f78166;
    animation: bounce 1.2s infinite;
    margin: 0 2px;
  }
  .loading-dots span:nth-child(2) { animation-delay: 0.2s; }
  .loading-dots span:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce {
    0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
    40% { transform: scale(1); opacity: 1; }
  }

  /* Input area */
  .input-area {
    padding: 12px 16px;
    background: #161b22;
    border-top: 1px solid #21262d;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .input-row {
    display: flex;
    gap: 8px;
    align-items: flex-end;
  }
  textarea {
    flex: 1;
    background: #0d0f14;
    border: 1px solid #30363d;
    border-radius: 6px;
    color: #c9d1d9;
    font-family: inherit;
    font-size: 12px;
    padding: 8px 10px;
    resize: none;
    min-height: 36px;
    max-height: 100px;
    outline: none;
    transition: border-color 0.15s;
    line-height: 1.5;
  }
  textarea:focus { border-color: #58a6ff; }
  textarea::placeholder { color: #484f58; }

  .btn-icon {
    width: 36px; height: 36px;
    border-radius: 6px;
    border: 1px solid #30363d;
    background: #21262d;
    color: #c9d1d9;
    cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px;
    transition: background 0.15s, border-color 0.15s;
    flex-shrink: 0;
  }
  .btn-icon:hover { background: #30363d; }
  .btn-icon.recording { background: #3d1a1a; border-color: #f85149; animation: recordPulse 1s infinite; }
  @keyframes recordPulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(248,81,73,0.4); }
    50% { box-shadow: 0 0 0 4px rgba(248,81,73,0); }
  }
  .btn-send {
    background: #238636;
    border-color: #238636;
    color: #fff;
  }
  .btn-send:hover { background: #2ea043; }

  .shortcuts {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .shortcut {
    padding: 3px 8px;
    background: #1c2128;
    border: 1px solid #30363d;
    border-radius: 4px;
    font-size: 10px;
    color: #6e7681;
    cursor: pointer;
    transition: border-color 0.15s, color 0.15s;
  }
  .shortcut:hover { border-color: #58a6ff; color: #58a6ff; }

  /* Voice indicator bar */
  .voice-bar {
    display: none;
    align-items: center;
    gap: 8px;
    font-size: 10px;
    color: #f85149;
    padding: 4px 0;
  }
  .voice-bar.active { display: flex; }
  .voice-wave { display: flex; align-items: center; gap: 2px; }
  .voice-wave span {
    display: block;
    width: 2px;
    background: #f85149;
    border-radius: 1px;
    animation: wave 0.8s ease-in-out infinite alternate;
  }
  .voice-wave span:nth-child(1) { height: 8px; animation-delay: 0s; }
  .voice-wave span:nth-child(2) { height: 14px; animation-delay: 0.1s; }
  .voice-wave span:nth-child(3) { height: 10px; animation-delay: 0.2s; }
  .voice-wave span:nth-child(4) { height: 16px; animation-delay: 0.3s; }
  .voice-wave span:nth-child(5) { height: 8px; animation-delay: 0.4s; }
  @keyframes wave {
    from { transform: scaleY(0.4); }
    to   { transform: scaleY(1); }
  }
</style>
</head>
<body>

<div class="header">
  <div class="header-logo">⚡</div>
  <div class="header-title">AWS Cheppu</div>
  <div class="header-subtitle">AWS Voice Control</div>
  <div class="status-dot"></div>
</div>

<div class="messages" id="messages">
  <div class="message system">
    <div class="message-label">System</div>
    <div class="message-bubble">AWS Cheppu ready. Speak or type to query your AWS environment.

Try: "Show idle EC2 instances" or "Show security groups"</div>
  </div>
</div>

<div class="input-area">
  <div class="shortcuts">
    <span class="shortcut" onclick="quickSend('Show idle EC2 instances')">EC2 status</span>
    <span class="shortcut" onclick="quickSend('Show EBS volumes')">EBS volumes</span>
    <span class="shortcut" onclick="quickSend('Show security groups')">Security groups</span>
    <span class="shortcut" onclick="quickSend('Show cost report')">Cost report</span>
  </div>
  <div class="voice-bar" id="voiceBar">
    <div class="voice-wave">
      <span></span><span></span><span></span><span></span><span></span>
    </div>
    Listening...
  </div>
  <div class="input-row">
    <button class="btn-icon" id="micBtn" title="Voice input (click to record)" onclick="toggleVoice()">🎤</button>
    <textarea
      id="inputBox"
      placeholder="Ask about your AWS infrastructure..."
      rows="1"
      onkeydown="handleKey(event)"
      oninput="autoResize(this)"
    ></textarea>
    <button class="btn-icon btn-send" title="Send" onclick="sendMessage()">↑</button>
  </div>
</div>

<audio id="audioPlayer" style="display:none"></audio>

<script>
  const vscode = acquireVsCodeApi();
  let isRecording = false;
  let mediaRecorder = null;
  let audioChunks = [];
  let pendingSessionId = null;

  // Handle messages from extension
  window.addEventListener('message', event => {
    const msg = event.data;
    switch (msg.command) {
      case 'addUserMessage':
        addMessage('user', '▸ You', msg.text);
        break;
      case 'addAiMessage':
        addMessage('ai', '⚡ VoiceOps', msg.text);
        break;
      case 'addConfirmationMessage':
        addConfirmation(msg.text, msg.sessionId, msg.risk);
        break;
      case 'setLoading':
        setLoading(msg.loading);
        break;
      case 'playAudio':
        playAudio(msg.url);
        break;
    }
  });

  function addMessage(type, label, text) {
    const msgs = document.getElementById('messages');
    const div = document.createElement('div');
    div.className = \`message \${type}\`;
    div.innerHTML = \`
      <div class="message-label">\${label}</div>
      <div class="message-bubble">\${escapeHtml(text)}</div>
    \`;
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function addConfirmation(text, sessionId, risk) {
    pendingSessionId = sessionId;
    const msgs = document.getElementById('messages');
    const div = document.createElement('div');
    div.className = 'message ai';
    div.innerHTML = \`
      <div class="message-label">⚡ VoiceOps — Confirmation Required</div>
      <div class="confirmation-card">
        <span class="risk-badge risk-\${risk || 'MEDIUM'}">\${risk || 'MEDIUM'} RISK</span>
        <div class="confirmation-text">\${escapeHtml(text)}</div>
        <div class="confirm-buttons">
          <button class="btn-confirm btn-yes" onclick="confirm(true)">✓ Proceed</button>
          <button class="btn-confirm btn-no" onclick="confirm(false)">✗ Cancel</button>
        </div>
      </div>
    \`;
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function confirm(yes) {
    if (!pendingSessionId) return;
    const sid = pendingSessionId;
    pendingSessionId = null;
    // Disable all confirm buttons
    document.querySelectorAll('.btn-confirm').forEach(b => b.disabled = true);
    addMessage('user', '▸ You', yes ? 'Yes, proceed' : 'No, cancel');
    vscode.postMessage({ command: 'confirm', sessionId: sid, confirmed: yes });
  }

  function setLoading(loading) {
    const existing = document.getElementById('loadingIndicator');
    if (loading && !existing) {
      const msgs = document.getElementById('messages');
      const div = document.createElement('div');
      div.id = 'loadingIndicator';
      div.className = 'loading-msg';
      div.innerHTML = \`
        <div class="loading-dots"><span></span><span></span><span></span></div>
        Processing...
      \`;
      msgs.appendChild(div);
      msgs.scrollTop = msgs.scrollHeight;
    } else if (!loading && existing) {
      existing.remove();
    }
  }

  function playAudio(url) {
    const player = document.getElementById('audioPlayer');
    player.src = url;
    player.play().catch(() => {});
  }

  function sendMessage() {
    const input = document.getElementById('inputBox');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    autoResize(input);
    vscode.postMessage({ command: 'sendMessage', text });
  }

  function quickSend(text) {
    document.getElementById('inputBox').value = text;
    sendMessage();
  }

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 100) + 'px';
  }

  async function toggleVoice() {
    if (isRecording) {
      stopRecording();
    } else {
      await startRecording();
    }
  }

  async function startRecording() {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaRecorder = new MediaRecorder(stream);
      audioChunks = [];
      mediaRecorder.ondataavailable = e => audioChunks.push(e.data);
      mediaRecorder.onstop = sendAudioToBackend;
      mediaRecorder.start();
      isRecording = true;
      document.getElementById('micBtn').classList.add('recording');
      document.getElementById('voiceBar').classList.add('active');
    } catch (err) {
      addMessage('system', 'System', '⚠️ Microphone access denied. Please allow microphone permissions.');
    }
  }

  function stopRecording() {
    if (mediaRecorder && isRecording) {
      mediaRecorder.stop();
      mediaRecorder.stream.getTracks().forEach(t => t.stop());
      isRecording = false;
      document.getElementById('micBtn').classList.remove('recording');
      document.getElementById('voiceBar').classList.remove('active');
    }
  }

  async function sendAudioToBackend() {
    const blob = new Blob(audioChunks, { type: 'audio/webm' });
    const formData = new FormData();
    formData.append('audio', blob, 'recording.webm');

    try {
      const res = await fetch('http://localhost:8000/transcribe', { method: 'POST', body: formData });
      const data = await res.json();
      if (data.text) {
        document.getElementById('inputBox').value = data.text;
        sendMessage();
      }
    } catch {
      addMessage('system', 'System', '⚠️ Could not connect to transcription service.');
    }
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(text));
    return div.innerHTML;
  }
</script>
</body>
</html>`;
  }

  public dispose() {
    ChatPanel.currentPanel = undefined;
    this._panel.dispose();
    while (this._disposables.length) {
      const x = this._disposables.pop();
      if (x) x.dispose();
    }
  }
}
