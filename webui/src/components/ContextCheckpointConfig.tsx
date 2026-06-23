import React, { useEffect, useState } from 'react';

const API_BASE = '';
const DEFAULT_THRESHOLD_K = 500;

const ContextCheckpointConfig: React.FC = () => {
  const [thresholdK, setThresholdK] = useState(DEFAULT_THRESHOLD_K);
  const [dirty, setDirty] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/api/context-checkpoint/config`)
      .then(r => r.json())
      .then(data => {
        if (data.threshold_k !== undefined) {
          setThresholdK(Number(data.threshold_k) || DEFAULT_THRESHOLD_K);
        }
      })
      .catch(() => {});
  }, []);

  const saveConfig = async () => {
    try {
      const response = await fetch(`${API_BASE}/api/context-checkpoint/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ threshold_k: thresholdK }),
      });
      const data = await response.json().catch(() => null);
      if (data?.config?.threshold_k !== undefined) {
        setThresholdK(Number(data.config.threshold_k) || DEFAULT_THRESHOLD_K);
      }
      setSaved(true);
      setDirty(false);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  const cardStyle: React.CSSProperties = {
    background: '#fff',
    padding: '24px',
    borderRadius: '8px',
  };

  const inputStyle: React.CSSProperties = {
    width: '110px',
    padding: '10px',
    border: '1px solid #ddd',
    borderRadius: '6px',
    fontSize: '14px',
    textAlign: 'center',
  };

  return (
    <div style={cardStyle}>
      <h2 style={{ marginBottom: '16px' }}>上下文自动压缩</h2>
      <p style={{ color: '#7f8c8d', fontSize: '13px', marginBottom: '20px', lineHeight: 1.7 }}>
        凌晨整理时扫描所有用户主聊天窗口；超过阈值后生成 context checkpoint，并在后续主聊天中作为系统上下文注入。
      </p>

      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
        <span style={{ fontWeight: 500 }}>超过</span>
        <input
          type="number"
          min={50}
          max={2000}
          step={10}
          value={thresholdK}
          onChange={e => {
            setThresholdK(Number(e.target.value));
            setDirty(true);
          }}
          style={inputStyle}
        />
        <span style={{ fontWeight: 500 }}>K 上下文时凌晨自动压缩</span>
      </div>

      <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginTop: '16px' }}>
        {[256, 500, 800, 1000].map(value => (
          <button
            key={value}
            onClick={() => {
              setThresholdK(value);
              setDirty(true);
            }}
            style={{
              padding: '6px 12px',
              border: '1px solid #ddd',
              borderRadius: '4px',
              background: '#fff',
              cursor: 'pointer',
              fontSize: '13px',
            }}
          >
            {value}K
          </button>
        ))}
      </div>

      <button
        onClick={saveConfig}
        disabled={!dirty}
        style={{
          marginTop: '18px',
          padding: '10px 24px',
          background: dirty ? '#3498db' : '#bdc3c7',
          color: '#fff',
          border: 'none',
          borderRadius: '6px',
          cursor: dirty ? 'pointer' : 'not-allowed',
          fontSize: '14px',
        }}
      >
        {saved ? '已保存 ✓' : '保存配置'}
      </button>
    </div>
  );
};

export default ContextCheckpointConfig;
