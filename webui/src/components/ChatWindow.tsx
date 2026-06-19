import React, { useState, useEffect, useRef, useCallback } from 'react';
import DebugStatusBar, { DebugStatus } from './DebugStatusBar';

interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  itemType?: 'text' | 'emoji' | 'meme' | 'search_meme';
  action?: string;
  visible?: boolean;
  isMeme?: boolean;
  memePath?: string;
  jobId?: string;
  snapshotId?: number;
  sendIndex?: number;
  sendCount?: number;
  sendKey?: string;
  timestamp: string;
}

interface StatusInfo extends DebugStatus {
  // 保留旧字段用于兼容
  status?: string;
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

const inferItemType = (text?: string, eventType?: string): Message['itemType'] => {
  if (text?.startsWith('meme:')) return 'meme';
  if (text?.startsWith('emoji:')) return 'emoji';
  if (eventType === 'assistant_react') return 'emoji';
  return 'text';
};

const ChatWindow: React.FC = () => {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [status, setStatus] = useState<StatusInfo | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [showSystem, setShowSystem] = useState(true);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const inputStatusTimerRef = useRef<number | null>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const fetchStatus = useCallback(() => {
    fetch(`${API_BASE}/api/status`)
      .then(r => r.json())
      .then(data => setStatus(data))
      .catch(() => {});
  }, []);

  const sendInputStatus = useCallback((composing: boolean, ttlMs = 3000) => {
    if (inputStatusTimerRef.current !== null) {
      window.clearTimeout(inputStatusTimerRef.current);
      inputStatusTimerRef.current = null;
    }

    const post = () => {
      fetch(`${API_BASE}/api/input-status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ composing, ttl_ms: ttlMs }),
      })
        .then(() => fetchStatus())
        .catch(() => {});
    };

    if (composing) {
      inputStatusTimerRef.current = window.setTimeout(post, 250);
    } else {
      post();
    }
  }, [fetchStatus]);

  // Load conversation history
  useEffect(() => {
    fetch(`${API_BASE}/api/conversation`)
      .then(r => r.json())
      .then(data => {
        const loaded = data.map((e: any, idx: number) => ({
          id: `hist_${idx}`,
          role: e.event_type?.startsWith('user') ? 'user' : 'assistant',
          text: e.text || '',
          itemType: inferItemType(e.text, e.event_type),
          action: e.action,
          isMeme: e.event_type === 'assistant_react' || e.text?.startsWith('meme:') || e.text?.startsWith('emoji:'),
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
          const content = data.content ?? data.text ?? '';
          const itemType = data.item_type ?? inferItemType(content);
          setMessages(prev => {
            if (data.send_key && prev.some(msg => msg.sendKey === data.send_key || msg.id === data.send_key)) {
              return prev;
            }
            return [...prev, {
              id: data.send_key || `msg_${Date.now()}_${prev.length}`,
              role: 'assistant',
              text: content,
              itemType,
              action: data.action,
              visible: data.visible,
              isMeme: data.is_meme || itemType === 'meme' || itemType === 'emoji',
              memePath: data.meme_path,
              jobId: data.job_id,
              snapshotId: data.snapshot_id,
              sendIndex: data.send_index,
              sendCount: data.send_count,
              sendKey: data.send_key,
              timestamp: new Date().toISOString(),
            }];
          });
          setIsLoading(false);
          fetchStatus();
        } else if (data.type === 'assistant_state') {
          if (data.result === 'llm_started') {
            // LLM 真正开始思考/生成，才显示“对方正在输入”
            setIsLoading(true);
          } else {
            setIsLoading(false);
          }
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

  useEffect(() => {
    const interval = setInterval(fetchStatus, 3000);
    fetchStatus();
    return () => clearInterval(interval);
  }, [fetchStatus]);

  useEffect(() => {
    return () => {
      if (inputStatusTimerRef.current !== null) {
        window.clearTimeout(inputStatusTimerRef.current);
      }
    };
  }, []);

  const handleInputChange = (value: string) => {
    setInput(value);
    sendInputStatus(value.trim().length > 0, 3000);
  };

  const sendMessage = async () => {
    if (!input.trim()) return;
    const text = input.trim();
    setInput('');

    setMessages(prev => [...prev, {
      id: `msg_${Date.now()}`,
      role: 'user',
      text,
      timestamp: new Date().toISOString(),
    }]);
    // 不立即显示“对方正在输入”：等到后端确认 LLM 真正启动后才显示

    try {
      if (inputStatusTimerRef.current !== null) {
        window.clearTimeout(inputStatusTimerRef.current);
        inputStatusTimerRef.current = null;
      }
      await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      });
    } catch {
      setIsLoading(false);
    } finally {
      sendInputStatus(false);
    }
  };

  const sendNudge = async () => {
    try {
      await fetch(`${API_BASE}/api/nudge`, { method: 'POST' });
    } catch {
      setIsLoading(false);
    }
  };

  const clearConversation = async () => {
    if (!confirm('确定要清空对话吗？')) return;
    sendInputStatus(false);
    await fetch(`${API_BASE}/api/conversation/clear`, { method: 'POST' });
    setMessages([]);
    setStatus(null);
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
              {msg.itemType === 'meme' && msg.text?.startsWith('meme:') ? (
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
              ) : msg.itemType === 'emoji' ? (
                <div style={{ fontSize: '24px', lineHeight: 1.2 }}>
                  {msg.text?.startsWith('emoji:') ? msg.text.slice(6) : msg.text}
                </div>
              ) : (
                <div>{msg.text}</div>
              )}
              {msg.action && (
                <div style={{ fontSize: '11px', opacity: 0.6, marginTop: '4px' }}>
                  action: {msg.action}
                  {msg.sendIndex !== undefined && msg.sendCount !== undefined && (
                    <>
                      {' · '}
                      #{msg.sendIndex + 1}/{msg.sendCount}
                    </>
                  )}
                  {msg.snapshotId !== undefined && (
                    <>
                      {' · '}
                      snapshot: {msg.snapshotId}
                    </>
                  )}
                  {msg.jobId && (
                    <>
                      {' · '}
                      job: {msg.jobId.slice(-14)}
                    </>
                  )}
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
            onChange={e => handleInputChange(e.target.value)}
            onBlur={() => sendInputStatus(false)}
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
            disabled={!input.trim()}
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

      {/* Debug status panel */}
      {showSystem && (
        <DebugStatusBar status={status} />
      )}
    </div>
  );
};

export default ChatWindow;
