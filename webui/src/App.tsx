import React, { useState } from 'react';
import ChatWindow from './components/ChatWindow';
import ApiConfig from './components/ApiConfig';
import SettingsPanel from './components/SettingsPanel';
import MemeManager from './components/MemeManager';

const tabs = [
  { id: 'chat', label: '对话' },
  { id: 'memes', label: '表情包' },
  { id: 'schedule', label: '其他配置' },
  { id: 'config', label: 'API配置' },
];

const App: React.FC = () => {
  const [activeTab, setActiveTab] = useState('chat');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <header style={{
        background: '#2c3e50',
        color: '#fff',
        padding: '12px 20px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
      }}>
        <h1 style={{ fontSize: '18px', fontWeight: 600 }}>Companion Bot</h1>
        <nav style={{ display: 'flex', gap: '4px' }}>
          {tabs.map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              style={{
                padding: '6px 14px',
                border: 'none',
                borderRadius: '4px',
                cursor: 'pointer',
                background: activeTab === tab.id ? '#3498db' : 'transparent',
                color: '#fff',
                fontSize: '14px',
              }}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </header>

      <main style={{ flex: 1, overflow: 'auto', padding: '16px' }}>
        {activeTab === 'chat' && <ChatWindow />}
        {activeTab === 'memes' && <MemeManager />}
        {activeTab === 'schedule' && <SettingsPanel />}
        {activeTab === 'config' && <ApiConfig />}
      </main>
    </div>
  );
};

export default App;
