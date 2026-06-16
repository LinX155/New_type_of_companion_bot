import React, { useState, useEffect } from 'react';

const API_BASE = '';

const ApiConfig: React.FC = () => {
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('https://api.xiaomimimo.com/v1');
  const [model, setModel] = useState('mimo-v2.5');
  const [hotDuration, setHotDuration] = useState(30);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/api/config`)
      .then(r => r.json())
      .then(data => {
        if (data.api_key) setApiKey(data.api_key);
        if (data.base_url) setBaseUrl(data.base_url);
        if (data.model) setModel(data.model);
      })
      .catch(() => {});

    fetch(`${API_BASE}/api/config/hot-duration`)
      .then(r => r.json())
      .then(data => {
        if (data.hot_duration_minutes !== undefined) {
          setHotDuration(data.hot_duration_minutes);
        }
      })
      .catch(() => {});
  }, []);

  const handleSave = async () => {
    try {
      await fetch(`${API_BASE}/api/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: apiKey, base_url: baseUrl, model }),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  const handleSaveHotDuration = async () => {
    try {
      await fetch(`${API_BASE}/api/config/hot-duration`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hot_duration_minutes: hotDuration }),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  return (
    <div style={{ maxWidth: '600px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* API Config */}
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
            {saved ? '已保存 ✓' : '保存配置'}
          </button>
        </div>
      </div>

      {/* Hot Duration Config */}
      <div style={{ background: '#fff', padding: '24px', borderRadius: '8px' }}>
        <h2 style={{ marginBottom: '16px' }}>状态机配置</h2>
        <p style={{ color: '#7f8c8d', fontSize: '13px', marginBottom: '16px' }}>
          设置热聊在场态（HOT）持续多久后自动退回离线生活态（COLD）。
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '16px' }}>
          <label style={{ fontWeight: 500 }}>HOT 持续时间：</label>
          <input
            type="number"
            min={1}
            max={1440}
            value={hotDuration}
            onChange={e => setHotDuration(Number(e.target.value))}
            style={{ width: '80px', padding: '10px', border: '1px solid #ddd', borderRadius: '6px', fontSize: '14px', textAlign: 'center' }}
          />
          <span>分钟</span>
        </div>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <button onClick={() => setHotDuration(1)} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>1分钟（测试）</button>
          <button onClick={() => setHotDuration(5)} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>5分钟</button>
          <button onClick={() => setHotDuration(10)} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>10分钟</button>
          <button onClick={() => setHotDuration(30)} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>30分钟</button>
          <button onClick={() => setHotDuration(60)} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>1小时</button>
        </div>
        <button
          onClick={handleSaveHotDuration}
          style={{
            marginTop: '16px',
            padding: '12px 24px',
            background: '#e74c3c',
            color: '#fff',
            border: 'none',
            borderRadius: '6px',
            cursor: 'pointer',
            fontSize: '14px',
            fontWeight: 500,
          }}
        >
          保存状态机配置
        </button>
      </div>
    </div>
  );
};

export default ApiConfig;
