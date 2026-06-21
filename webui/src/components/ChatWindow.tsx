import React, { useState, useEffect, useRef, useCallback } from 'react';
import DebugStatusBar, { DebugStatus } from './DebugStatusBar';

interface Message {
  id: string;
  role: 'user' | 'assistant' | 'system';
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
  onebot?: {
    connected?: boolean;
  };
  gate?: {
    pending_job_id: string | null;
    stale_jobs_count: number;
    sent_jobs_count: number;
    buffered_events: number;
  };
}

interface SessionInfo {
  session_id: string;
  platform?: string;
  user_id?: string;
  label?: string;
  message_count?: number;
  last_message_at?: string | null;
}

const API_BASE = '';
const DEFAULT_SESSION_ID = 'webui_default';

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
  const [selectedSessionId, setSelectedSessionId] = useState(
    () => window.localStorage.getItem('chat_session_id') || DEFAULT_SESSION_ID
  );
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const inputStatusTimerRef = useRef<number | null>(null);
  const wsReconnectTimerRef = useRef<number | null>(null);
  const conversationLoadSeqRef = useRef(0);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const fetchStatus = useCallback(() => {
    fetch(`${API_BASE}/api/status?session_id=${encodeURIComponent(selectedSessionId)}`)
      .then(r => r.json())
      .then(data => setStatus(data))
      .catch(() => {});
  }, [selectedSessionId]);

  const loadConversation = useCallback(() => {
    const loadSeq = conversationLoadSeqRef.current + 1;
    conversationLoadSeqRef.current = loadSeq;
    return fetch(`${API_BASE}/api/conversation?session_id=${encodeURIComponent(selectedSessionId)}`)
      .then(r => r.json())
      .then(data => {
        if (loadSeq !== conversationLoadSeqRef.current) {
          return;
        }
        const loaded = data.map((e: any, idx: number) => ({
          id: e.id ? `hist_${e.id}` : `hist_${idx}`,
          role: e.event_type === 'nudge' ? 'system' : e.event_type?.startsWith('user') ? 'user' : 'assistant',
          text: e.event_type === 'nudge' ? '已拍一拍' : e.text || '',
          itemType: inferItemType(e.text, e.event_type),
          action: e.action,
          isMeme: e.event_type === 'assistant_react' || e.text?.startsWith('meme:') || e.text?.startsWith('emoji:'),
          timestamp: e.created_at,
        }));
        setMessages(loaded);
      })
      .catch(() => {});
  }, [selectedSessionId]);

  const loadSessions = useCallback(() => {
    fetch(`${API_BASE}/api/sessions`)
      .then(r => r.json())
      .then(data => {
        const loaded = Array.isArray(data.sessions) ? data.sessions : [];
        if (!loaded.some((item: SessionInfo) => item.session_id === selectedSessionId)) {
          loaded.unshift({ session_id: selectedSessionId, label: selectedSessionId });
        }
        setSessions(loaded);
      })
      .catch(() => {});
  }, [selectedSessionId]);

  const sendInputStatus = useCallback((composing: boolean, ttlMs = 3000) => {
    if (inputStatusTimerRef.current !== null) {
      window.clearTimeout(inputStatusTimerRef.current);
      inputStatusTimerRef.current = null;
    }

    const post = () => {
      fetch(`${API_BASE}/api/input-status`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ composing, ttl_ms: ttlMs, session_id: selectedSessionId }),
      })
        .then(() => fetchStatus())
        .catch(() => {});
    };

    if (composing) {
      inputStatusTimerRef.current = window.setTimeout(post, 250);
    } else {
      post();
    }
  }, [fetchStatus, selectedSessionId]);

  useEffect(() => {
    loadConversation();
    loadSessions();
  }, [loadConversation, loadSessions]);

  // WebSocket
  useEffect(() => {
    let disposed = false;

    const connect = () => {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);
      wsRef.current = ws;
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'conversation_changed') {
            loadSessions();
            if (!data.session_id || data.session_id === selectedSessionId) {
              loadConversation();
            }
            fetchStatus();
          } else if (data.type === 'assistant_message') {
            if (data.session_id && data.session_id !== selectedSessionId) {
              loadSessions();
              return;
            }
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
        if (wsRef.current === ws) {
          wsRef.current = null;
        }
        if (!disposed) {
          wsReconnectTimerRef.current = window.setTimeout(connect, 1500);
        }
      };
      ws.onerror = () => {
        ws.close();
      };
    };

    connect();

    return () => {
      disposed = true;
      if (wsReconnectTimerRef.current !== null) {
        window.clearTimeout(wsReconnectTimerRef.current);
        wsReconnectTimerRef.current = null;
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [fetchStatus, loadConversation, loadSessions, selectedSessionId]);

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
        body: JSON.stringify({ text, session_id: selectedSessionId }),
      });
    } catch {
      setIsLoading(false);
    } finally {
      sendInputStatus(false);
    }
  };

  const sendNudge = async () => {
    try {
      await fetch(`${API_BASE}/api/nudge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: selectedSessionId }),
      });
      setMessages(prev => [...prev, {
        id: `nudge_${Date.now()}_${prev.length}`,
        role: 'system',
        text: '已拍一拍',
        timestamp: new Date().toISOString(),
      }]);
      fetchStatus();
    } catch {
      setIsLoading(false);
    }
  };

  const clearConversation = async () => {
    if (!confirm(`确定要清空当前会话 ${selectedSessionId} 的对话吗？`)) return;
    sendInputStatus(false);
    await fetch(`${API_BASE}/api/conversation/clear?session_id=${encodeURIComponent(selectedSessionId)}`, { method: 'POST' });
    setMessages([]);
    setStatus(null);
  };

  const deleteCurrentSession = async () => {
    if (selectedSessionId === DEFAULT_SESSION_ID) {
      alert('webui_default 不能完全删除，可以使用清空对话。');
      return;
    }
    if (!confirm(`确定完全清除当前用户 ${selectedSessionId} 吗？这会删除该用户私有历史、日志、运行状态和记忆目录。`)) return;
    if (!confirm('再次确认：这个操作不会删除 SOUL 和全局表情包，但该用户私有数据会被抹除。')) return;
    await fetch(`${API_BASE}/api/sessions/${encodeURIComponent(selectedSessionId)}`, { method: 'DELETE' });
    const nextSession = DEFAULT_SESSION_ID;
    window.localStorage.setItem('chat_session_id', nextSession);
    setSelectedSessionId(nextSession);
    setMessages([]);
    setStatus(null);
    loadSessions();
  };

  const handleSessionChange = (sessionId: string) => {
    const nextSession = sessionId || DEFAULT_SESSION_ID;
    window.localStorage.setItem('chat_session_id', nextSession);
    setSelectedSessionId(nextSession);
    setMessages([]);
    setStatus(null);
  };

  const napcatConnected = status?.onebot?.connected === true;

  return (
    <div style={{
      display: 'grid',
      gridTemplateColumns: showSystem ? 'minmax(520px, 1fr) 652px' : 'minmax(520px, 760px)',
      gap: '16px',
      height: '100%',
      minHeight: 0,
      alignItems: 'stretch',
      overflow: 'auto',
    }}>
      {/* Chat area */}
      <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', gap: '12px', minHeight: 0 }}>
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
          <div style={{
            alignSelf: 'flex-start',
            position: 'sticky',
            top: 0,
            zIndex: 1,
            display: 'inline-flex',
            alignItems: 'center',
            gap: '7px',
            padding: '5px 9px',
            borderRadius: '999px',
            background: 'rgba(255, 255, 255, 0.94)',
            border: '1px solid #e6ebef',
            color: '#5d6d7e',
            fontSize: '12px',
            lineHeight: 1.2,
            boxShadow: '0 1px 4px rgba(44, 62, 80, 0.08)',
          }}>
            <span style={{
              width: '8px',
              height: '8px',
              borderRadius: '50%',
              background: napcatConnected ? '#2ecc71' : '#bdc3c7',
              boxShadow: napcatConnected ? '0 0 0 3px rgba(46, 204, 113, 0.16)' : 'none',
            }} />
            <span>Napcat连接状态</span>
            <select
              value={selectedSessionId}
              onChange={e => handleSessionChange(e.target.value)}
              style={{
                marginLeft: '8px',
                border: '1px solid #d8dee4',
                borderRadius: '6px',
                padding: '3px 6px',
                fontSize: '12px',
                background: '#fff',
                color: '#2c3e50',
              }}
              title="当前会话"
            >
              {sessions.map(session => (
                <option key={session.session_id} value={session.session_id}>
                  {session.label || session.session_id}
                </option>
              ))}
            </select>
          </div>
          {messages.map(msg => (
            <div key={msg.id} style={{
              alignSelf: msg.role === 'system' ? 'center' : msg.role === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: msg.role === 'system' ? 'none' : '70%',
              background: msg.role === 'system' ? '#f4f6f7' : msg.role === 'user' ? '#3498db' : '#ecf0f1',
              color: msg.role === 'system' ? '#7f8c8d' : msg.role === 'user' ? '#fff' : '#2c3e50',
              padding: msg.role === 'system' ? '5px 10px' : '10px 14px',
              borderRadius: msg.role === 'system' ? '999px' : msg.role === 'user' ? '16px 16px 4px 16px' : '16px 16px 16px 4px',
              fontSize: msg.role === 'system' ? '12px' : '14px',
              lineHeight: msg.role === 'system' ? 1.2 : 1.5,
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
            onClick={deleteCurrentSession}
            disabled={selectedSessionId === DEFAULT_SESSION_ID}
            style={{
              padding: '8px 16px',
              background: selectedSessionId === DEFAULT_SESSION_ID ? '#bdc3c7' : '#c0392b',
              color: '#fff',
              border: 'none',
              borderRadius: '8px',
              cursor: selectedSessionId === DEFAULT_SESSION_ID ? 'not-allowed' : 'pointer',
              fontSize: '14px',
            }}
          >
            完全清除当前用户
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
        <div style={{ minWidth: 0, minHeight: 0 }}>
          <DebugStatusBar status={status} />
        </div>
      )}
    </div>
  );
};

export default ChatWindow;
