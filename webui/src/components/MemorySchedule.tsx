import React, { useEffect, useState } from 'react';

const API_BASE = '';

type ActiveMonitorRow = {
  session_id: string;
  next_time_to_activate: string;
};

type ActiveMonitor = {
  enabled: boolean;
  generated_at?: string;
  rows: ActiveMonitorRow[];
};

const MemorySchedule: React.FC = () => {
  const [dayHour, setDayHour] = useState(13);
  const [dayMinute, setDayMinute] = useState(0);
  const [nightHour, setNightHour] = useState(19);
  const [nightMinute, setNightMinute] = useState(0);
  const [cleanupHour, setCleanupHour] = useState(3);
  const [cleanupMinute, setCleanupMinute] = useState(30);
  const [saved, setSaved] = useState(false);
  const [dirtySchedule, setDirtySchedule] = useState(false);

  const [hotDuration, setHotDuration] = useState(30);
  const [savedHot, setSavedHot] = useState(false);
  const [dirtyHot, setDirtyHot] = useState(false);

  const [activeEnabled, setActiveEnabled] = useState(false);
  const [activeHour, setActiveHour] = useState(10);
  const [activeMinute, setActiveMinute] = useState(0);
  const [activeDailyLimit, setActiveDailyLimit] = useState(1);
  const [savedActive, setSavedActive] = useState(false);
  const [dirtyActive, setDirtyActive] = useState(false);
  const [activeMonitor, setActiveMonitor] = useState<ActiveMonitor | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/api/memory/schedule`)
      .then(r => r.json())
      .then(data => {
        if (data.memory_analysis_day) {
          setDayHour(data.memory_analysis_day.hour);
          setDayMinute(data.memory_analysis_day.minute);
        }
        if (data.memory_analysis_night) {
          setNightHour(data.memory_analysis_night.hour);
          setNightMinute(data.memory_analysis_night.minute);
        }
        if (data.midnight_cleanup) {
          setCleanupHour(data.midnight_cleanup.hour);
          setCleanupMinute(data.midnight_cleanup.minute);
        }
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

    fetch(`${API_BASE}/api/active-message/config`)
      .then(r => r.json())
      .then(data => {
        setActiveEnabled(Boolean(data.enabled));
        setActiveHour(data.hour ?? 10);
        setActiveMinute(data.minute ?? 0);
        setActiveDailyLimit(data.daily_limit ?? 1);
      })
      .catch(() => {});

    loadActiveMonitor();
  }, []);

  const loadActiveMonitor = async () => {
    try {
      const response = await fetch(`${API_BASE}/api/active-message/monitor`);
      const data = await response.json();
      setActiveMonitor({
        enabled: Boolean(data.enabled),
        generated_at: data.generated_at,
        rows: Array.isArray(data.rows) ? data.rows : [],
      });
    } catch {
      setActiveMonitor(null);
    }
  };

  const handleSave = async () => {
    try {
      await fetch(`${API_BASE}/api/memory/schedule`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          memory_analysis_day_hour: dayHour,
          memory_analysis_day_minute: dayMinute,
          memory_analysis_night_hour: nightHour,
          memory_analysis_night_minute: nightMinute,
          midnight_cleanup_hour: cleanupHour,
          midnight_cleanup_minute: cleanupMinute,
        }),
      });
      setSaved(true);
      setDirtySchedule(false);
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
      setSavedHot(true);
      setDirtyHot(false);
      setTimeout(() => setSavedHot(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  const handleSaveActiveMessage = async () => {
    try {
      const response = await fetch(`${API_BASE}/api/active-message/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: activeEnabled,
          hour: activeHour,
          minute: activeMinute,
          daily_limit: activeDailyLimit,
        }),
      });
      const data = await response.json();
      if (data.config) {
        setActiveEnabled(Boolean(data.config.enabled));
        setActiveHour(data.config.hour ?? activeHour);
        setActiveMinute(data.config.minute ?? activeMinute);
        setActiveDailyLimit(data.config.daily_limit ?? activeDailyLimit);
      }
      setSavedActive(true);
      setDirtyActive(false);
      setTimeout(() => setSavedActive(false), 2000);
      loadActiveMonitor();
    } catch {
      alert('保存失败');
    }
  };

  const timeInputStyle: React.CSSProperties = {
    width: '60px',
    padding: '8px',
    border: '1px solid #dfe5e8',
    borderRadius: '6px',
    fontSize: '14px',
    textAlign: 'center',
  };

  const pageStyle: React.CSSProperties = {
    width: '100%',
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
    gap: '16px',
    alignItems: 'stretch',
  };

  const cardStyle: React.CSSProperties = {
    background: '#fff',
    padding: '22px',
    borderRadius: '8px',
    border: '1px solid #edf0f2',
    minWidth: 0,
    display: 'flex',
    flexDirection: 'column',
  };

  const sectionStackStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'column',
    gap: '18px',
    flex: 1,
  };

  const rowStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    flexWrap: 'wrap',
  };

  const labelStyle: React.CSSProperties = {
    fontSize: '13px',
    color: '#52616b',
    marginBottom: '8px',
    fontWeight: 600,
  };

  const buttonStyle = (dirty: boolean, accent = '#3498db'): React.CSSProperties => ({
    padding: '10px 20px',
    background: dirty ? accent : '#bdc3c7',
    color: '#fff',
    border: 'none',
    borderRadius: '6px',
    cursor: dirty ? 'pointer' : 'not-allowed',
    fontSize: '14px',
  });

  const monitorText = () => {
    if (!activeMonitor) {
      return <div style={{ color: '#7f8c8d' }}>监控数据读取中</div>;
    }
    if (!activeMonitor.enabled) {
      return <div style={{ color: '#7f8c8d' }}>主动消息未开启</div>;
    }
    if (!activeMonitor.rows.length) {
      return <div style={{ color: '#7f8c8d' }}>暂无可监控用户</div>;
    }
    return activeMonitor.rows.map(row => (
      <div
        key={row.session_id}
        style={{
          fontFamily: 'Consolas, Monaco, monospace',
          whiteSpace: 'nowrap',
        }}
      >
        {row.session_id}:{row.next_time_to_activate}
      </div>
    ));
  };

  return (
    <div style={pageStyle}>
      <div style={cardStyle}>
        <h2 style={{ marginBottom: '18px' }}>主动消息配置</h2>

        <div style={sectionStackStyle}>
          <label style={{ ...rowStyle, fontSize: '14px', fontWeight: 600 }}>
            <input
              type="checkbox"
              checked={activeEnabled}
              onChange={e => { setActiveEnabled(e.target.checked); setDirtyActive(true); }}
            />
            启用主动消息
          </label>

          <div>
            <div style={labelStyle}>全局默认时间</div>
            <div style={rowStyle}>
              <input
                type="number"
                min={0}
                max={23}
                value={activeHour}
                onChange={e => { setActiveHour(Number(e.target.value)); setDirtyActive(true); }}
                style={timeInputStyle}
              />
              <span>:</span>
              <input
                type="number"
                min={0}
                max={59}
                value={activeMinute}
                onChange={e => { setActiveMinute(Number(e.target.value)); setDirtyActive(true); }}
                style={timeInputStyle}
              />
            </div>
          </div>

          <div>
            <div style={labelStyle}>用户下次主动消息</div>
            <div style={{
              padding: '12px',
              minHeight: '96px',
              maxHeight: '220px',
              overflow: 'auto',
              background: '#f8f9fa',
              borderRadius: '6px',
              border: '1px solid #edf0f2',
              color: '#2c3e50',
              fontSize: '13px',
              lineHeight: 1.8,
            }}>
              {monitorText()}
            </div>
          </div>
        </div>

        <div style={{ marginTop: '22px' }}>
          <button
            onClick={handleSaveActiveMessage}
            disabled={!dirtyActive}
            style={buttonStyle(dirtyActive)}
          >
            {savedActive ? '已保存' : '保存配置'}
          </button>
        </div>
      </div>

      <div style={cardStyle}>
        <h2 style={{ marginBottom: '18px' }}>记忆线程时间配置</h2>

        <div style={sectionStackStyle}>
          <div>
            <div style={labelStyle}>日间记忆分析</div>
            <div style={rowStyle}>
              <input type="number" min={0} max={23} value={dayHour} onChange={e => { setDayHour(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
              <span>:</span>
              <input type="number" min={0} max={59} value={dayMinute} onChange={e => { setDayMinute(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
            </div>
          </div>

          <div>
            <div style={labelStyle}>晚间记忆分析</div>
            <div style={rowStyle}>
              <input type="number" min={0} max={23} value={nightHour} onChange={e => { setNightHour(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
              <span>:</span>
              <input type="number" min={0} max={59} value={nightMinute} onChange={e => { setNightMinute(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
            </div>
          </div>

          <div>
            <div style={labelStyle}>凌晨整理线程</div>
            <div style={rowStyle}>
              <input type="number" min={0} max={23} value={cleanupHour} onChange={e => { setCleanupHour(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
              <span>:</span>
              <input type="number" min={0} max={59} value={cleanupMinute} onChange={e => { setCleanupMinute(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
            </div>
          </div>
        </div>

        <div style={{ marginTop: '22px' }}>
          <button
            onClick={handleSave}
            disabled={!dirtySchedule}
            style={buttonStyle(dirtySchedule)}
          >
            {saved ? '已保存' : '保存配置'}
          </button>
        </div>
      </div>

      <div style={cardStyle}>
        <h2 style={{ marginBottom: '18px' }}>状态机配置</h2>

        <div style={sectionStackStyle}>
          <div>
            <div style={labelStyle}>HOT 持续时间</div>
            <div style={rowStyle}>
              <input
                type="number"
                min={1}
                max={1440}
                value={hotDuration}
                onChange={e => { setHotDuration(Number(e.target.value)); setDirtyHot(true); }}
                style={{ ...timeInputStyle, width: '80px' }}
              />
              <span>分钟</span>
            </div>
          </div>

          <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
            {[1, 5, 10, 30, 60].map(value => (
              <button
                key={value}
                onClick={() => { setHotDuration(value); setDirtyHot(true); }}
                style={{
                  padding: '6px 12px',
                  border: '1px solid #dfe5e8',
                  borderRadius: '4px',
                  background: '#fff',
                  cursor: 'pointer',
                  fontSize: '13px',
                }}
              >
                {value === 60 ? '1小时' : `${value}分钟`}
              </button>
            ))}
          </div>
        </div>

        <div style={{ marginTop: '22px' }}>
          <button
            onClick={handleSaveHotDuration}
            disabled={!dirtyHot}
            style={buttonStyle(dirtyHot, '#e74c3c')}
          >
            {savedHot ? '已保存' : '保存配置'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default MemorySchedule;
