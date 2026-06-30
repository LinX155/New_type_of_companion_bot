import React from 'react';
import SoulEditor from './SoulEditor';
import MemorySchedule from './MemorySchedule';
import ContextCheckpointConfig from './ContextCheckpointConfig';
import GroupChatOpsConfig from './GroupChatOpsConfig';

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
    <GroupChatOpsConfig />
    <ContextCheckpointConfig />
  </div>
);

export default SettingsPanel;
