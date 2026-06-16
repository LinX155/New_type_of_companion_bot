import React, { useState, useEffect, useRef, useCallback } from 'react';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  action?: string;
  visible?: boolean;
  isMeme?: boolean;
  memePath?: string;
  timestamp: string;
}

interface StatusInfo {
  status: string;
  buffer_version?: number;
  msg_index_today?: number;
  hot_until?: string;
  hot_duration_minutes?: number;
  last_action?: string;
  last_text?: string;
  last_snapshot_result?: string;
  gate?: {
    pending_job_id: string | null;
    stale_jobs_count: number;
    sent_jobs_count: number;
    buffered_events: number;
  };
}

const API_BASE = '';

const ChatWindow: React.FC = () => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [status, setStatus] = useState<StatusInfo | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [showSystem, setShowSystem] = useState(true);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // Load conversation history
  useEffect(() => {
    fetch(`${API_BASE}/api/conversation`)
      .then(r => r.json())
      .then(data => {
        const loaded = data.map((e: any, idx: number) => ({
          id: `hist_${idx}`,
          role: e.event_type?.startsWith('user') ? 'user' : 'assistant',
          text: e.text || '',
          action: e.action,
          isMeme: e.event_type === 'assistant_react' || e.text?.startsWith('meme:'),
          timestamp: e.created_at,
        }));
        setMessages(loaded);
      })
      .catch(() => {});
  }, []);

  // WebSocket
  useEffect(() => {
    const ws = new WebSocket(`ws://${window.location.host}/ws`);
    wsRef.current = ws;
    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'assistant_message') {
          setMessages(prev => [...prev, {
            id: `msg_${Date.now()}`,
            role: 'assistant',
            text: data.text || '',
            action: data.action,
            visible: data.visible,
            isMeme: data.is_meme,
            memePath: data.meme_path,
            timestamp: new Date().toISOString(),
          }]);
          setIsLoading(false);
          fetchStatus();
        } else if (data.type === 'assistant_state') {
          setIsLoading(false);
          fetchStatus();
        }
      } catch {}
    };
    ws.onclose = () => {
      setTimeout(() => {
        // reconnect
      }, 3000);
    };
    return () => ws.close();
  }, []);

  const fetchStatus = useCallback(() => {
    fetch(`${API_BASE}/api/status`)
      .then(r => r.json())
      .then(data => setStatus(data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const interval = setInterval(fetchStatus, 3000);
    fetchStatus();
    return () => clearInterval(interval);
  }, [fetchStatus]);

  const sendMessage = async () => {
    if (!input.trim()) return;
    const text = input.trim();
    setInput('');
    setIsLoading(true);

    setMessages(prev => [...prev, {
      id: `msg_${Date.now()}`,
      role: 'user',
      text,
      timestamp: new Date().toISOString(),
    }]);

    try {
      await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
    } catch {
      setIsLoading(false);
    }
  };

  const sendNudge = async () => {
    setIsLoading(true);
    try {
      await fetch(`${API_BASE}/api/nudge`, { method: 'POST' });
    } catch {
      setIsLoading(false);
    }
  };

  const clearConversation = async () => {
    if (!confirm('确定要清空对话吗？')) return;
    await fetch(`${API_BASE}/api/conversation/clear`, { method: 'POST' });
    setMessages([]);
    setStatus(null);
  };

  const renderMemeUrl = (memePath: string) => {
    // memePath is file_stem, we need to find category
    // For now use a generic endpoint or infer from stem
    return `${API_BASE}/api/memes/image/_/${memePath}`; // fallback
  };

  return (
    <div style={{ display: 'flex', gap: '16px', height: '100%' }}>
      {/* Chat area */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {/* Messages */}
        <div style={{
          flex: 1,
          overflowY: 'auto',
          background: '#fff',
          borderRadius: '8px',
          padding: '16px',
          display: 'flex',
          flexDirection: 'column',
          gap: '12px',
        }}>
          {messages.map(msg => (
            <div key={msg.id} style={{
              alignSelf: msg.role === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: '70%',
              background: msg.role === 'user' ? '#3498db' : '#ecf0f1',
              color: msg.role === 'user' ? '#fff' : '#2c3e50',
              padding: '10px 14px',
              borderRadius: msg.role === 'user' ? '16px 16px 4px 16px' : '16px 16px 16px 4px',
              fontSize: '14px',
              lineHeight: 1.5,
            }}>
              {msg.isMeme && msg.text?.startsWith('meme:') ? (
                <div>
                  <img
                    src={`${API_BASE}/api/memes/render?stem=${encodeURIComponent(msg.text.slice(5))}`}
                    alt="meme"
                    style={{ maxWidth: '200px', maxHeight: '200px', borderRadius: '8px', display: 'block' }}
                    onError={(e) => {
                      // fallback: try to find via generic search
                      const img = e.currentTarget;
                      img.src = `${API_BASE}/api/memes/image/_/${msg.text?.slice(5)}`;
                    }}
                  />
                  <span style={{ fontSize: '12px', opacity: 0.7, marginTop: '4px', display: 'block' }}>
                    {msg.text}
                  </span>
                </div>
              ) : (
                <div>{msg.text}</div>
              )}
              {msg.action && (
                <div style={{ fontSize: '11px', opacity: 0.6, marginTop: '4px' }}>
                  action: {msg.action}
                </div>
              )}
            </div>
          ))}
          {isLoading && (
            <div style={{ alignSelf: 'flex-start', color: '#95a5a6', fontSize: '14px' }}>
              对方正在输入...
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div style={{ display: 'flex', gap: '8px' }}>
          <input
            type="text"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && sendMessage()}
            placeholder="输入消息..."
            style={{
              flex: 1,
              padding: '12px 16px',
              border: '1px solid #ddd',
              borderRadius: '8px',
              fontSize: '14px',
              outline: 'none',
            }}
          />
          <button
            onClick={sendMessage}
            disabled={isLoading}
            style={{
              padding: '12px 24px',
              background: '#3498db',
              color: '#fff',
              border: 'none',
              borderRadius: '8px',
              cursor: 'pointer',
              fontSize: '14px',
            }}
          >
            发送
          </button>
        </div>

        <div style={{ display: 'flex', gap: '8px' }}>
          <button
            onClick={sendNudge}
            disabled={isLoading}
            style={{
              padding: '8px 16px',
              background: '#e74c3c',
              color: '#fff',
              border: 'none',
              borderRadius: '8px',
              cursor: 'pointer',
              fontSize: '14px',
            }}
          >
            戳一戳 / 拍一拍 👋
          </button>
          <button
            onClick={clearConversation}
            style={{
              padding: '8px 16px',
              background: '#95a5a6',
              color: '#fff',
              border: 'none',
              borderRadius: '8px',
              cursor: 'pointer',
              fontSize: '14px',
            }}
          >
            清空对话
          </button>
          <button
            onClick={() => setShowSystem(!showSystem)}
            style={{
              padding: '8px 16px',
              background: '#7f8c8d',
              color: '#fff',
              border: 'none',
              borderRadius: '8px',
              cursor: 'pointer',
              fontSize: '14px',
            }}
          >
            {showSystem ? '隐藏' : '显示'}系统状态
          </button>
        </div>
      </div>

      {/* System status panel */}
      {showSystem && (
        <div style={{ width: '280px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {/* LLM / 对话层状态 */}
          <div style={{
            background: '#fff',
            borderRadius: '8px',
            padding: '16px',
            fontSize: '13px',
            lineHeight: 1.6,
          }}>
            <h3 style={{ marginBottom: '12px', fontSize: '15px', color: '#2c3e50' }}>对话层 (LLM)</h3>
            {status ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                <div><strong>状态:</strong> <span style={{ color: status.status === 'HOT' ? '#e74c3c' : '#3498db', fontWeight: 600 }}>{status.status}</span></div>
                <div><strong>今日消息数:</strong> {status.msg_index_today}</div>
                <div><strong>HOT持续时间:</strong> {status.hot_duration_minutes}分钟</div>
                {status.hot_until && (
                  <div><strong>HOT剩余:</strong> {Math.max(0, Math.ceil((new Date(status.hot_until).getTime() - Date.now()) / 60000))}分钟</div>
                )}
                <div><strong>最后动作:</strong> {status.last_action || '-'}</div>
                <div><strong>最后文本:</strong> {status.last_text ? (status.last_text.length > 30 ? status.last_text.slice(0, 30) + '...' : status.last_text) : '-'}</div>
                <div><strong>最后结果:</strong> {status.last_snapshot_result || '-'}</div>
              </div>
            ) : (
              <div style={{ color: '#95a5a6' }}>加载中...</div>
            )}
          </div>

          {/* EventGate 实时事件门状态 */}
          <div style={{
            background: '#fff',
            borderRadius: '8px',
            padding: '16px',
            fontSize: '13px',
            lineHeight: 1.6,
          }}>
            <h3 style={{ marginBottom: '12px', fontSize: '15px', color: '#2c3e50' }}>事件门 (EventGate)</h3>
            {status?.gate ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                <div><strong>缓冲区事件:</strong> {status.gate.buffered_events}</div>
                <div><strong>当前Job:</strong> {status.gate.pending_job_id ? status.gate.pending_job_id.slice(-10) : '无'}</div>
                <div><strong>已作废Job数:</strong> {status.gate.stale_jobs_count}</div>
                <div><strong>已发送Job数:</strong> {status.gate.sent_jobs_count}</div>
              </div>
            ) : (
              <div style={{ color: '#95a5a6' }}>加载中...</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default ChatWindow;
