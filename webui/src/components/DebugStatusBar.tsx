import React from 'react';

interface ColdStartMeta {
  timestamp: string;
  last_user_message_age?: string;
  msg_index: number;
  status: string;
}

interface SnapshotPanel {
  snapshot_id: number;
  buffer_version: number;
  status: string;
  buffered_events: number;
  memory_sources: string[];
  cold_start_meta?: ColdStartMeta;
  hot_until?: string;
  hot_remaining?: number;
  context_note?: string;
}

interface LlmPanel {
  last_llm_raw?: string;
  last_parsed_action?: string;
  last_parsed_text?: string;
  parse_status?: string;
  decision_result?: string;
}

interface GatePanel {
  pending_job_id: string | null;
  buffered_events: number;
  stale_jobs_count: number;
  sent_jobs_count: number;
}

interface MemePanel {
  search_meme?: string;
  candidates?: string[];
  selected_meme?: string;
  render_status?: string;
  last_command?: string;
  command_result?: string;
}

export interface DebugStatus {
  snapshot?: SnapshotPanel;
  llm?: LlmPanel;
  gate?: GatePanel;
  meme?: MemePanel | null;
}

interface Props {
  status: DebugStatus | null;
}

const cardStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: '8px',
  padding: '16px',
  fontSize: '13px',
  lineHeight: 1.6,
};

const cardTitleStyle: React.CSSProperties = {
  marginBottom: '12px',
  fontSize: '15px',
  color: '#2c3e50',
  fontWeight: 600,
};

const rowStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: '4px',
};

const labelStyle: React.CSSProperties = {
  color: '#7f8c8d',
  fontSize: '12px',
};

const valueStyle: React.CSSProperties = {
  color: '#2c3e50',
  wordBreak: 'break-word',
};

const badgeStyle = (color: string): React.CSSProperties => ({
  display: 'inline-block',
  padding: '2px 8px',
  borderRadius: '4px',
  background: color,
  color: '#fff',
  fontSize: '12px',
  fontWeight: 600,
});

const statusColor = (status?: string) => {
  if (status === 'HOT') return '#e74c3c';
  if (status === 'COLD') return '#3498db';
  return '#95a5a6';
};

const parseStatusColor = (status?: string) => {
  if (status === 'ok') return '#27ae60';
  if (status === 'fallback') return '#f39c12';
  if (status === 'error') return '#e74c3c';
  return '#95a5a6';
};

const renderStatusColor = (status?: string) => {
  if (status === 'hit') return '#27ae60';
  if (status === 'miss') return '#e74c3c';
  if (status === 'fallback') return '#f39c12';
  return '#95a5a6';
};

const Field: React.FC<{ label: string; value?: React.ReactNode }> = ({ label, value }) => (
  <div style={rowStyle}>
    <span style={labelStyle}>{label}</span>
    <span style={valueStyle}>{value ?? '-'}</span>
  </div>
);

const DebugStatusBar: React.FC<Props> = ({ status }) => {
  if (!status) {
    return (
      <div style={{ width: '320px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
        <div style={cardStyle}>加载中...</div>
      </div>
    );
  }

  const snapshot = status.snapshot;
  const llm = status.llm;
  const gate = status.gate;
  const meme = status.meme;

  return (
    <div style={{ width: '320px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
      {/* 面板 1：当前 Snapshot / 主回复对象 */}
      <div style={cardStyle}>
        <h3 style={cardTitleStyle}>当前 Snapshot / 主回复对象</h3>
        {snapshot ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
              <Field label="snapshot_id" value={snapshot.snapshot_id} />
              <Field label="buffer_version" value={snapshot.buffer_version} />
            </div>
            <Field
              label="status"
              value={<span style={badgeStyle(statusColor(snapshot.status))}>{snapshot.status}</span>}
            />
            <Field label="buffered_events" value={snapshot.buffered_events} />
            <Field label="memory_sources" value={snapshot.memory_sources.join(' / ')} />

            {snapshot.status === 'COLD' && snapshot.cold_start_meta ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                <div style={{ ...labelStyle, color: '#3498db' }}>
                  以下 cold_start_meta 会传入 LLM
                </div>
                <Field label="timestamp" value={snapshot.cold_start_meta.timestamp} />
                <Field label="last_user_message_age" value={snapshot.cold_start_meta.last_user_message_age} />
                <Field label="msg_index" value={snapshot.cold_start_meta.msg_index} />
                <Field label="status" value={snapshot.cold_start_meta.status} />
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                <div style={{ ...labelStyle, color: '#e74c3c' }}>
                  热聊状态，只传递普通聊天上下文，不传递冷启动元信息
                </div>
                <Field label="hot_until" value={snapshot.hot_until ? new Date(snapshot.hot_until).toLocaleString() : '-'} />
                <Field label="hot_remaining" value={snapshot.hot_remaining !== undefined ? `${snapshot.hot_remaining} 分钟` : '-'} />
                {snapshot.context_note && <div style={{ color: '#7f8c8d', fontSize: '12px' }}>{snapshot.context_note}</div>}
              </div>
            )}

            <div style={{ borderTop: '1px solid #ecf0f1', paddingTop: '10px' }}>
              <div style={{ ...labelStyle, marginBottom: '8px' }}>LLM 决策结果</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                <Field label="last_llm_raw" value={llm?.last_llm_raw ? (llm.last_llm_raw.length > 80 ? llm.last_llm_raw.slice(0, 80) + '...' : llm.last_llm_raw) : '-'} />
                <Field label="last_parsed_action" value={llm?.last_parsed_action} />
                <Field label="last_parsed_text" value={llm?.last_parsed_text ? (llm.last_parsed_text.length > 40 ? llm.last_parsed_text.slice(0, 40) + '...' : llm.last_parsed_text) : '-'} />
                <Field
                  label="parse_status"
                  value={<span style={badgeStyle(parseStatusColor(llm?.parse_status))}>{llm?.parse_status ?? '-'}</span>}
                />
                <Field
                  label="decision_result"
                  value={<span style={badgeStyle(statusColor(llm?.decision_result === 'sent' ? 'HOT' : llm?.decision_result === 'dropped' ? 'COLD' : undefined))}>{llm?.decision_result ?? '-'}</span>}
                />
              </div>
            </div>
          </div>
        ) : (
          <div style={{ color: '#95a5a6' }}>无 Snapshot 数据</div>
        )}
      </div>

      {/* 面板 2：实时事件门 / 发送权 */}
      <div style={cardStyle}>
        <h3 style={cardTitleStyle}>实时事件门 / 发送权</h3>
        {gate ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <Field label="pending_job_id" value={gate.pending_job_id ? gate.pending_job_id.slice(-14) : '无'} />
            <Field label="buffered_events" value={gate.buffered_events} />
            <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
              <Field label="stale_jobs_count" value={gate.stale_jobs_count} />
              <Field label="sent_jobs_count" value={gate.sent_jobs_count} />
            </div>
          </div>
        ) : (
          <div style={{ color: '#95a5a6' }}>无事件门数据</div>
        )}
      </div>

      {/* 面板 3：Meme / 命令链路 */}
      {meme && (
        <div style={cardStyle}>
          <h3 style={cardTitleStyle}>Meme / 命令链路</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {meme.search_meme && <Field label="search_meme" value={meme.search_meme} />}
            {meme.candidates && meme.candidates.length > 0 && (
              <Field label="candidates" value={meme.candidates.slice(0, 5).join(', ')} />
            )}
            {meme.selected_meme && <Field label="selected_meme" value={meme.selected_meme} />}
            {meme.render_status && (
              <Field
                label="render_status"
                value={<span style={badgeStyle(renderStatusColor(meme.render_status))}>{meme.render_status}</span>}
              />
            )}
            {meme.last_command && (
              <>
                <Field label="last_command" value={meme.last_command} />
                <Field label="command_result" value={meme.command_result} />
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default DebugStatusBar;
