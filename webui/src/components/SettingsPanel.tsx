import React from 'react';
import SoulEditor from './SoulEditor';
import MemorySchedule from './MemorySchedule';

const SettingsPanel: React.FC = () => (
  <div style={{
    width: '100%',
    maxWidth: '1280px',
    margin: '0 auto',
    display: 'flex',
    flexDirection: 'column',
    gap: '20px',
  }}>
    <SoulEditor />
    <MemorySchedule />
  </div>
);

export default SettingsPanel;
