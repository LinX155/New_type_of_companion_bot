import React, { useState, useEffect } from 'react';

const API_BASE = '';

const SoulEditor: React.FC = () => {
  const [content, setContent] = useState('');
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/api/soul`)
      .then(r => r.json())
      .then(data => setContent(data.content || ''))
      .catch(() => {});
  }, []);

  const handleSave = async () => {
    try {
      await fetch(`${API_BASE}/api/soul`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  return (
    <div style={{ maxWidth: '800px', background: '#fff', padding: '24px', borderRadius: '8px' }}>
      <h2 style={{ marginBottom: '16px' }}>SOUL.md 配置</h2>
      <p style={{ color: '#7f8c8d', fontSize: '13px', marginBottom: '16px' }}>
        定义角色的稳定偏好、边界和说话习惯。支持 Markdown 格式。
      </p>
      <textarea
        value={content}
        onChange={e => setContent(e.target.value)}
        style={{
          width: '100%',
          minHeight: '400px',
          padding: '12px',
          border: '1px solid #ddd',
          borderRadius: '6px',
          fontSize: '14px',
          fontFamily: 'monospace',
          lineHeight: 1.6,
          resize: 'vertical',
        }}
      />
      <div style={{ marginTop: '16px', display: 'flex', gap: '12px', alignItems: 'center' }}>
        <button
          onClick={handleSave}
          style={{
            padding: '10px 24px',
            background: '#3498db',
            color: '#fff',
            border: 'none',
            borderRadius: '6px',
            cursor: 'pointer',
            fontSize: '14px',
          }}
        >
          {saved ? '已保存 ✓' : '保存 SOUL'}
        </button>
        {saved && <span style={{ color: '#27ae60', fontSize: '14px' }}>SOUL 已更新</span>}
      </div>
    </div>
  );
};

export default SoulEditor;
