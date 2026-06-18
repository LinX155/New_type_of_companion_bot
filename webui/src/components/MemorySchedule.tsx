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

  const timeInputStyle = {
    width: '60px',
    padding: '8px',
    border: '1px solid #ddd',
    borderRadius: '6px',
    fontSize: '14px',
    textAlign: 'center' as const,
  };

  return (
    <div style={{ maxWidth: '600px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div style={{ background: '#fff', padding: '24px', borderRadius: '8px' }}>
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

      <div style={{ background: '#fff', padding: '24px', borderRadius: '8px' }}>
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
