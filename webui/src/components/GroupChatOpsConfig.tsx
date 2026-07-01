import React, { useEffect, useMemo, useState } from 'react';

const API_BASE = '';

type GroupPolicy = {
  observe_only?: boolean;
  allow_mention_reply?: boolean;
  allow_roll_reply?: boolean;
  allow_repetition?: boolean;
  allow_meme_send?: boolean;
  allow_active_message?: boolean;
  allow_command_reply?: boolean;
  configured?: boolean;
};

type GroupConfigResponse = {
  status?: string;
  config?: {
    enabled?: boolean;
  };
  group_id?: string | null;
  known_group_ids?: string[];
  group_policy?: GroupPolicy | null;
};

const currentGroupIdFromStorage = () => {
  const sessionId = window.localStorage.getItem('chat_session_id') || '';
  return sessionId.startsWith('qq_group_') ? sessionId.slice('qq_group_'.length) : '';
};

const GroupChatOpsConfig: React.FC = () => {
  const initialGroupId = currentGroupIdFromStorage();
  const [globalEnabled, setGlobalEnabled] = useState(false);
  const [groupId, setGroupId] = useState(initialGroupId);
  const [knownGroupIds, setKnownGroupIds] = useState<string[]>([]);
  const [observeOnly, setObserveOnly] = useState(true);
  const [allowMention, setAllowMention] = useState(true);
  const [allowRoll, setAllowRoll] = useState(false);
  const [allowRepetition, setAllowRepetition] = useState(false);
  const [allowMeme, setAllowMeme] = useState(false);
  const [allowActiveMessage, setAllowActiveMessage] = useState(false);
  const [allowCommand, setAllowCommand] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [saved, setSaved] = useState(false);

  const selectedGroupId = useMemo(() => groupId.trim(), [groupId]);

  const applyResponse = (data: GroupConfigResponse) => {
    setGlobalEnabled(Boolean(data.config?.enabled));
    if (data.group_id) {
      setGroupId(data.group_id);
    }
    setKnownGroupIds(Array.isArray(data.known_group_ids) ? data.known_group_ids : []);
    const policy = data.group_policy || {};
    setObserveOnly(policy.observe_only !== false);
    setAllowMention(policy.allow_mention_reply !== false);
    setAllowRoll(Boolean(policy.allow_roll_reply));
    setAllowRepetition(Boolean(policy.allow_repetition));
    setAllowMeme(Boolean(policy.allow_meme_send));
    setAllowActiveMessage(Boolean(policy.allow_active_message));
    setAllowCommand(policy.allow_command_reply !== false);
    setDirty(false);
  };

  const loadConfig = async (targetGroupId = selectedGroupId) => {
    const query = targetGroupId ? `?group_id=${encodeURIComponent(targetGroupId)}` : '';
    try {
      const response = await fetch(`${API_BASE}/api/group-chat/config${query}`);
      const data = await response.json();
      applyResponse(data);
    } catch {
      // keep current local form state
    }
  };

  useEffect(() => {
    loadConfig(initialGroupId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const saveConfig = async () => {
    if (!selectedGroupId) {
      alert('请先填写群号');
      return;
    }
    try {
      const response = await fetch(`${API_BASE}/api/group-chat/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: globalEnabled,
          group_id: selectedGroupId,
          observe_only: observeOnly,
          allow_mention_reply: allowMention,
          allow_roll_reply: allowRoll,
          allow_repetition: allowRepetition,
          allow_meme_send: allowMeme,
          allow_active_message: allowActiveMessage,
          allow_command_reply: allowCommand,
        }),
      });
      const data = await response.json();
      applyResponse(data);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  const cardStyle: React.CSSProperties = {
    background: '#fff',
    padding: '22px',
    borderRadius: '8px',
    border: '1px solid #edf0f2',
  };

  const rowStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    flexWrap: 'wrap',
  };

  const inputStyle: React.CSSProperties = {
    width: '180px',
    padding: '9px 10px',
    border: '1px solid #dfe5e8',
    borderRadius: '6px',
    fontSize: '14px',
  };

  const checkboxLabel = (label: string, checked: boolean, onChange: (value: boolean) => void, disabled = false) => (
    <label style={{
      display: 'flex',
      alignItems: 'center',
      gap: '8px',
      color: disabled ? '#95a5a6' : '#2c3e50',
      fontSize: '14px',
      minWidth: '148px',
    }}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={e => {
          onChange(e.target.checked);
          setDirty(true);
        }}
      />
      {label}
    </label>
  );

  return (
    <div style={cardStyle}>
      <h2 style={{ marginBottom: '16px' }}>群聊运营开关</h2>
      <div style={{ color: '#7f8c8d', fontSize: '13px', lineHeight: 1.7, marginBottom: '18px' }}>
        新群默认处于观察期，只记录群聊窗口、记忆和表情包入库观察；结束观察期后，再按各项能力开关决定是否回复。
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        <label style={{ ...rowStyle, fontSize: '14px', fontWeight: 600 }}>
          <input
            type="checkbox"
            checked={globalEnabled}
            onChange={e => {
              setGlobalEnabled(e.target.checked);
              setDirty(true);
            }}
          />
          启用群聊发送总开关
        </label>

        <div style={rowStyle}>
          <span style={{ fontSize: '13px', fontWeight: 600, color: '#52616b' }}>群号</span>
          <input
            value={groupId}
            onChange={e => {
              setGroupId(e.target.value);
              setDirty(true);
            }}
            placeholder="例如 123456"
            style={inputStyle}
          />
          <button
            onClick={() => loadConfig(selectedGroupId)}
            style={{
              padding: '8px 14px',
              border: '1px solid #dfe5e8',
              borderRadius: '6px',
              background: '#fff',
              cursor: 'pointer',
              fontSize: '13px',
            }}
          >
            读取
          </button>
          {knownGroupIds.length > 0 && (
            <select
              value={knownGroupIds.includes(selectedGroupId) ? selectedGroupId : ''}
              onChange={e => {
                setGroupId(e.target.value);
                setDirty(false);
                loadConfig(e.target.value);
              }}
              style={inputStyle}
            >
              <option value="">已知群</option>
              {knownGroupIds.map(id => (
                <option key={id} value={id}>{id}</option>
              ))}
            </select>
          )}
        </div>

        <div style={{ ...rowStyle, padding: '12px', background: '#f8f9fa', borderRadius: '6px', border: '1px solid #edf0f2' }}>
          {checkboxLabel('观察期 / 只读', observeOnly, setObserveOnly)}
          {checkboxLabel('允许 @ 回复', allowMention, setAllowMention, observeOnly)}
          {checkboxLabel('允许 roll 回复', allowRoll, setAllowRoll, observeOnly)}
          {checkboxLabel('允许复读', allowRepetition, setAllowRepetition, observeOnly)}
          {checkboxLabel('允许发表情包', allowMeme, setAllowMeme, observeOnly)}
          {checkboxLabel('允许主动消息', allowActiveMessage, setAllowActiveMessage, observeOnly)}
          {checkboxLabel('允许命令回执', allowCommand, setAllowCommand, observeOnly)}
        </div>
      </div>

      <button
        onClick={saveConfig}
        disabled={!dirty}
        style={{
          marginTop: '18px',
          padding: '10px 22px',
          background: dirty ? '#3498db' : '#bdc3c7',
          color: '#fff',
          border: 'none',
          borderRadius: '6px',
          cursor: dirty ? 'pointer' : 'not-allowed',
          fontSize: '14px',
        }}
      >
        {saved ? '已保存' : '保存配置'}
      </button>
    </div>
  );
};

export default GroupChatOpsConfig;
