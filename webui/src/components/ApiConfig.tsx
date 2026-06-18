import React, { useState, useEffect } from 'react';

const API_BASE = '';

const ApiConfig: React.FC = () => {
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('https://api.deepseek.com');
  const [model, setModel] = useState('deepseek-v4-flash');
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [savedApi, setSavedApi] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/api/config`)
      .then(r => r.json())
      .then(data => {
        if (data.api_key) setApiKey(data.api_key);
        if (data.base_url) setBaseUrl(data.base_url);
        if (data.model) setModel(data.model);
        if (data.thinking_enabled !== undefined) setThinkingEnabled(data.thinking_enabled);
      })
      .catch(() => {});
  }, []);

  const saveConfig = async (thinkingValue: boolean) => {
    await fetch(`${API_BASE}/api/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: apiKey, base_url: baseUrl, model, thinking_enabled: thinkingValue }),
    });
  };

  const handleSave = async () => {
    try {
      await saveConfig(thinkingEnabled);
      setSavedApi(true);
      setTimeout(() => setSavedApi(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  const handleToggleThinking = async (next: boolean) => {
    setThinkingEnabled(next);
    try {
      await saveConfig(next);
      setSavedApi(true);
      setTimeout(() => setSavedApi(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  return (
    <div style={{ maxWidth: '600px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div style={{ background: '#fff', padding: '24px', borderRadius: '8px' }}>
        <h2 style={{ marginBottom: '20px' }}>大模型 API 配置</h2>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <div>
            <label style={{ display: 'block', marginBottom: '6px', fontWeight: 500 }}>API Key</label>
            <input
              type="text"
              value={apiKey}
              onChange={e => setApiKey(e.target.value)}
              placeholder="输入 API Key..."
              style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '6px', fontSize: '14px' }}
            />
          </div>
          <div>
            <label style={{ display: 'block', marginBottom: '6px', fontWeight: 500 }}>Base URL</label>
            <input
              type="text"
              value={baseUrl}
              onChange={e => setBaseUrl(e.target.value)}
              style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '6px', fontSize: '14px' }}
            />
          </div>
          <div>
            <label style={{ display: 'block', marginBottom: '6px', fontWeight: 500 }}>Model</label>
            <input
              type="text"
              value={model}
              onChange={e => setModel(e.target.value)}
              style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '6px', fontSize: '14px' }}
            />
          </div>

          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '12px',
            background: '#f8f9fa',
            borderRadius: '6px',
          }}>
            <div>
              <div style={{ fontWeight: 500, fontSize: '14px' }}>是否开启思考模式</div>
              <div style={{ color: '#7f8c8d', fontSize: '12px', marginTop: '2px' }}>
                开启后 LLM 会先思考再回复（更慢、更强），关闭则直接快速回复。
              </div>
            </div>
            <label style={{ position: 'relative', display: 'inline-block', width: '44px', height: '24px', flexShrink: 0 }}>
              <input
                type="checkbox"
                checked={thinkingEnabled}
                onChange={e => handleToggleThinking(e.target.checked)}
                style={{ opacity: 0, width: 0, height: 0 }}
              />
              <span style={{
                position: 'absolute',
                cursor: 'pointer',
                top: 0, left: 0, right: 0, bottom: 0,
                background: thinkingEnabled ? '#27ae60' : '#ccc',
                borderRadius: '24px',
                transition: '0.2s',
              }}>
                <span style={{
                  position: 'absolute',
                  height: '18px', width: '18px',
                  left: thinkingEnabled ? '23px' : '3px',
                  bottom: '3px',
                  background: '#fff',
                  borderRadius: '50%',
                  transition: '0.2s',
                }} />
              </span>
            </label>
          </div>

          <button
            onClick={handleSave}
            style={{
              padding: '12px',
              background: '#3498db',
              color: '#fff',
              border: 'none',
              borderRadius: '6px',
              cursor: 'pointer',
              fontSize: '14px',
              fontWeight: 500,
            }}
          >
            {savedApi ? '已保存 ✓' : '保存配置'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ApiConfig;
