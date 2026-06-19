import React, { useState, useEffect } from 'react';

const API_BASE = '';

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
  const [quietStartHour, setQuietStartHour] = useState(0);
  const [quietEndHour, setQuietEndHour] = useState(9);
  const [savedActive, setSavedActive] = useState(false);
  const [dirtyActive, setDirtyActive] = useState(false);
  const [activeStatus, setActiveStatus] = useState<any>(null);
  const [activeRunResult, setActiveRunResult] = useState('');

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
        setQuietStartHour(data.quiet_start_hour ?? 0);
        setQuietEndHour(data.quiet_end_hour ?? 9);
      })
      .catch(() => {});

    loadActiveStatus();
  }, []);

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

  const loadActiveStatus = async () => {
    try {
      const response = await fetch(`${API_BASE}/api/active-message/status`);
      const data = await response.json();
      setActiveStatus(data);
    } catch {}
  };

  const handleSaveActiveMessage = async () => {
    try {
      await fetch(`${API_BASE}/api/active-message/config`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: activeEnabled,
          hour: activeHour,
          minute: activeMinute,
          daily_limit: activeDailyLimit,
          quiet_start_hour: quietStartHour,
          quiet_end_hour: quietEndHour,
        }),
      });
      setSavedActive(true);
      setDirtyActive(false);
      setTimeout(() => setSavedActive(false), 2000);
      loadActiveStatus();
    } catch {
      alert('保存失败');
    }
  };

  const handleRunActiveMessage = async () => {
    setActiveRunResult('检查中');
    try {
      const response = await fetch(`${API_BASE}/api/active-message/run`, { method: 'POST' });
      const data = await response.json();
      setActiveRunResult(data.status === 'sent' ? '已发送' : `未发送：${data.reason || data.status}`);
      loadActiveStatus();
    } catch {
      setActiveRunResult('检查失败');
    }
  };

  const timeInputStyle = {
    width: '60px',
    padding: '8px',
    border: '1px solid #ddd',
    borderRadius: '6px',
    fontSize: '14px',
    textAlign: 'center' as const,
  };

  const pageStyle: React.CSSProperties = {
    width: '100%',
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
    gap: '20px',
    alignItems: 'start',
  };

  const cardStyle: React.CSSProperties = {
    background: '#fff',
    padding: '24px',
    borderRadius: '8px',
    minWidth: 0,
  };

  return (
    <div style={pageStyle}>
      <div style={cardStyle}>
        <h2 style={{ marginBottom: '16px' }}>记忆线程时间配置</h2>
        <p style={{ color: '#7f8c8d', fontSize: '13px', marginBottom: '20px' }}>
          设置定时记忆分析任务和凌晨整理线程的运行时间。
        </p>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          <div>
            <h3 style={{ fontSize: '15px', marginBottom: '10px' }}>日间记忆分析</h3>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input type="number" min={0} max={23} value={dayHour} onChange={e => { setDayHour(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
              <span>:</span>
              <input type="number" min={0} max={59} value={dayMinute} onChange={e => { setDayMinute(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
            </div>
          </div>

          <div>
            <h3 style={{ fontSize: '15px', marginBottom: '10px' }}>晚间记忆分析</h3>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input type="number" min={0} max={23} value={nightHour} onChange={e => { setNightHour(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
              <span>:</span>
              <input type="number" min={0} max={59} value={nightMinute} onChange={e => { setNightMinute(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
            </div>
          </div>

          <div>
            <h3 style={{ fontSize: '15px', marginBottom: '10px' }}>凌晨整理线程</h3>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input type="number" min={0} max={23} value={cleanupHour} onChange={e => { setCleanupHour(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
              <span>:</span>
              <input type="number" min={0} max={59} value={cleanupMinute} onChange={e => { setCleanupMinute(Number(e.target.value)); setDirtySchedule(true); }} style={timeInputStyle} />
            </div>
          </div>
        </div>

        <div style={{ marginTop: '24px' }}>
          <button
            onClick={handleSave}
            disabled={!dirtySchedule}
            style={{
              padding: '10px 24px',
              background: dirtySchedule ? '#3498db' : '#bdc3c7',
              color: '#fff',
              border: 'none',
              borderRadius: '6px',
              cursor: dirtySchedule ? 'pointer' : 'not-allowed',
              fontSize: '14px',
            }}
          >
            {saved ? '已保存 ✓' : '保存配置'}
          </button>
        </div>
      </div>

      <div style={cardStyle}>
        <h2 style={{ marginBottom: '16px' }}>主动消息配置</h2>
        <p style={{ color: '#7f8c8d', fontSize: '13px', marginBottom: '20px' }}>
          主动消息每天按固定时间检查一次，只在空闲、未超上限且有 pending 候选时发送。
        </p>

        <label style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '18px', fontSize: '14px' }}>
          <input
            type="checkbox"
            checked={activeEnabled}
            onChange={e => { setActiveEnabled(e.target.checked); setDirtyActive(true); }}
          />
          启用主动消息
        </label>

        <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
          <div>
            <h3 style={{ fontSize: '15px', marginBottom: '10px' }}>每日检查时间</h3>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input type="number" min={0} max={23} value={activeHour} onChange={e => { setActiveHour(Number(e.target.value)); setDirtyActive(true); }} style={timeInputStyle} />
              <span>:</span>
              <input type="number" min={0} max={59} value={activeMinute} onChange={e => { setActiveMinute(Number(e.target.value)); setDirtyActive(true); }} style={timeInputStyle} />
            </div>
          </div>

          <div>
            <h3 style={{ fontSize: '15px', marginBottom: '10px' }}>每日上限</h3>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input
                type="number"
                min={1}
                max={3}
                value={activeDailyLimit}
                onChange={e => { setActiveDailyLimit(Number(e.target.value)); setDirtyActive(true); }}
                style={timeInputStyle}
              />
              <span>条</span>
            </div>
          </div>

          <div>
            <h3 style={{ fontSize: '15px', marginBottom: '10px' }}>免打扰时段</h3>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <input type="number" min={0} max={23} value={quietStartHour} onChange={e => { setQuietStartHour(Number(e.target.value)); setDirtyActive(true); }} style={timeInputStyle} />
              <span>点 到</span>
              <input type="number" min={0} max={23} value={quietEndHour} onChange={e => { setQuietEndHour(Number(e.target.value)); setDirtyActive(true); }} style={timeInputStyle} />
              <span>点</span>
            </div>
          </div>
        </div>

        <div style={{
          marginTop: '20px',
          padding: '12px',
          background: '#f8f9fa',
          borderRadius: '6px',
          border: '1px solid #edf0f2',
          color: '#2c3e50',
          fontSize: '13px',
          lineHeight: 1.7,
        }}>
          <div>今日已发：{activeStatus?.sent_today ?? 0} 条</div>
          <div>未回复退避：{activeStatus?.has_unanswered_active_message ? '生效中' : '未触发'}</div>
          <div>EventGate：{activeStatus?.gate_ready ? '空闲' : '不可发送'}</div>
          <div>
            候选：pending {activeStatus?.candidates?.status?.pending ?? 0} /
            used {activeStatus?.candidates?.status?.used ?? 0} /
            blocked {activeStatus?.candidates?.status?.blocked ?? 0} /
            expired {activeStatus?.candidates?.status?.expired ?? 0}
          </div>
        </div>

        <div style={{ marginTop: '18px', display: 'flex', gap: '8px', flexWrap: 'wrap', alignItems: 'center' }}>
          <button
            onClick={handleSaveActiveMessage}
            disabled={!dirtyActive}
            style={{
              padding: '10px 24px',
              background: dirtyActive ? '#3498db' : '#bdc3c7',
              color: '#fff',
              border: 'none',
              borderRadius: '6px',
              cursor: dirtyActive ? 'pointer' : 'not-allowed',
              fontSize: '14px',
            }}
          >
            {savedActive ? '已保存 ✓' : '保存配置'}
          </button>
          <button
            onClick={handleRunActiveMessage}
            disabled={!activeEnabled}
            style={{
              padding: '10px 18px',
              background: activeEnabled ? '#2c3e50' : '#bdc3c7',
              color: '#fff',
              border: 'none',
              borderRadius: '6px',
              cursor: activeEnabled ? 'pointer' : 'not-allowed',
              fontSize: '14px',
            }}
          >
            立即检查一次
          </button>
          {activeRunResult && <span style={{ color: '#7f8c8d', fontSize: '13px' }}>{activeRunResult}</span>}
        </div>
      </div>

      <div style={cardStyle}>
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
            onChange={e => { setHotDuration(Number(e.target.value)); setDirtyHot(true); }}
            style={{ width: '80px', padding: '10px', border: '1px solid #ddd', borderRadius: '6px', fontSize: '14px', textAlign: 'center' }}
          />
          <span>分钟</span>
        </div>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <button onClick={() => { setHotDuration(1); setDirtyHot(true); }} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>1分钟（测试）</button>
          <button onClick={() => { setHotDuration(5); setDirtyHot(true); }} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>5分钟</button>
          <button onClick={() => { setHotDuration(10); setDirtyHot(true); }} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>10分钟</button>
          <button onClick={() => { setHotDuration(30); setDirtyHot(true); }} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>30分钟</button>
          <button onClick={() => { setHotDuration(60); setDirtyHot(true); }} style={{ padding: '6px 12px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer', fontSize: '13px' }}>1小时</button>
        </div>
        <button
          onClick={handleSaveHotDuration}
          disabled={!dirtyHot}
          style={{
            marginTop: '16px',
            padding: '12px 24px',
            background: dirtyHot ? '#e74c3c' : '#bdc3c7',
            color: '#fff',
            border: 'none',
            borderRadius: '6px',
            cursor: dirtyHot ? 'pointer' : 'not-allowed',
            fontSize: '14px',
            fontWeight: 500,
          }}
        >
          {savedHot ? '已保存 ✓' : '保存状态机配置'}
        </button>
      </div>
    </div>
  );
};

export default MemorySchedule;
