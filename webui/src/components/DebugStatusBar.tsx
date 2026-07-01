import React from 'react';

interface SnapshotPanel {
  status: string;
  buffered_events: number;
}

interface LlmPanel {
  last_llm_raw?: string;
  parse_status?: string;
  decision_result?: string;
}

interface GatePanel {
  pending_job_id: string | null;
  buffered_events: number;
  user_composing?: unknown;
}

interface MemePanel {
  search_meme?: string;
  candidates?: unknown[];
  selected_meme?: string;
  render_status?: string;
  last_command?: string;
  command_result?: string;
}

interface GroupMemoryPanel {
  path?: string;
  dm_path?: string;
  latest_dm_path?: string;
  latest_daytime_memory?: unknown;
  latest_midnight_cleanup?: unknown;
  latest_command?: unknown;
}

export interface DebugStatus {
  status?: string;
  last_action?: string;
  snapshot?: SnapshotPanel;
  llm?: LlmPanel;
  gate?: GatePanel;
  meme?: MemePanel | null;
  group_memory?: GroupMemoryPanel | null;
}

interface Props {
  status: DebugStatus | null;
}

const panelStyle: React.CSSProperties = {
  background: '#fff',
  borderRadius: '8px',
  padding: '14px',
  fontSize: '13px',
  lineHeight: 1.5,
  minWidth: 0,
  minHeight: 0,
  display: 'flex',
  flexDirection: 'column',
};

const titleStyle: React.CSSProperties = {
  margin: '0 0 10px',
  fontSize: '14px',
  color: '#2c3e50',
  fontWeight: 700,
};

const codeStyle: React.CSSProperties = {
  margin: 0,
  padding: '10px',
  borderRadius: '6px',
  background: '#0b0f14',
  color: '#e6edf3',
  fontFamily: 'Consolas, Monaco, "Courier New", monospace',
  fontSize: '12px',
  lineHeight: 1.45,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  flex: 1,
  minHeight: 0,
  overflow: 'auto',
};

const formatJson = (value: unknown) => JSON.stringify(value, null, 2);

const rawText = (value?: string | null) => value && value.trim() ? value : '-';

const CodeBlock: React.FC<{ children: string }> = ({ children }) => (
  <pre style={codeStyle}>{children}</pre>
);

const DebugStatusBar: React.FC<Props> = ({ status }) => {
  if (!status) {
    return (
      <div style={{ width: '100%', display: 'grid', gridTemplateColumns: '1fr 1fr', gridTemplateRows: '1fr 1fr', gap: '12px', minHeight: 0 }}>
        <div style={panelStyle}>加载中...</div>
      </div>
    );
  }

  const snapshot = status.snapshot;
  const gate = status.gate;
  const llm = status.llm;
  const meme = status.meme;

  const sessionPayload = {
    status: snapshot?.status ?? status.status ?? null,
    buffered_events: gate?.buffered_events ?? snapshot?.buffered_events ?? null,
    pending_job_id: gate?.pending_job_id ?? null,
    user_composing: gate?.user_composing ?? null,
    group_memory: status.group_memory ? {
      path: status.group_memory.path ?? null,
      dm_path: status.group_memory.dm_path ?? null,
      latest_dm_path: status.group_memory.latest_dm_path ?? null,
      latest_daytime_memory: status.group_memory.latest_daytime_memory ?? null,
      latest_midnight_cleanup: status.group_memory.latest_midnight_cleanup ?? null,
      latest_command: status.group_memory.latest_command ?? null,
    } : null,
  };

  const execPayload = {
    decision_result: llm?.decision_result ?? null,
    last_action: status.last_action ?? null,
    parse_status: llm?.parse_status ?? null,
  };

  const memePayload = meme ? {
    search_meme: meme.search_meme ?? null,
    candidates: meme.candidates ?? [],
    selected_meme: meme.selected_meme ?? null,
    render_status: meme.render_status ?? null,
    last_command: meme.last_command ?? null,
    command_result: meme.command_result ?? null,
  } : {
    search_meme: null,
    candidates: [],
    selected_meme: null,
    render_status: null,
    last_command: null,
    command_result: null,
  };

  return (
    <div style={{
      width: '100%',
      height: '100%',
      minHeight: 0,
      display: 'grid',
      gridTemplateColumns: 'minmax(0, 1fr) minmax(0, 1fr)',
      gridTemplateRows: 'minmax(0, 1.1fr) minmax(0, 0.9fr)',
      gap: '12px',
      alignItems: 'stretch',
    }}>
      <section style={{ ...panelStyle, gridColumn: 1, gridRow: 1 }}>
        <h3 style={titleStyle}>LLM 原始输出</h3>
        <CodeBlock>{rawText(llm?.last_llm_raw)}</CodeBlock>
      </section>

      <section style={{ ...panelStyle, gridColumn: 1, gridRow: 2 }}>
        <h3 style={titleStyle}>执行结果</h3>
        <CodeBlock>{formatJson(execPayload)}</CodeBlock>
      </section>

      <section style={{ ...panelStyle, gridColumn: 2, gridRow: 1 }}>
        <h3 style={titleStyle}>表情日志</h3>
        <CodeBlock>{formatJson(memePayload)}</CodeBlock>
      </section>

      <section style={{ ...panelStyle, gridColumn: 2, gridRow: 2 }}>
        <h3 style={titleStyle}>会话</h3>
        <CodeBlock>{formatJson(sessionPayload)}</CodeBlock>
      </section>
    </div>
  );
};

export default DebugStatusBar;
