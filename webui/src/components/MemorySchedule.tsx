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
      setTimeout(() => setSaved(false), 2000);
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
    <div style={{ maxWidth: '600px', background: '#fff', padding: '24px', borderRadius: '8px' }}>
      <h2 style={{ marginBottom: '16px' }}>记忆线程时间配置</h2>
      <p style={{ color: '#7f8c8d', fontSize: '13px', marginBottom: '20px' }}>
        设置定时记忆分析任务和凌晨整理线程的运行时间。
      </p>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
        <div>
          <h3 style={{ fontSize: '15px', marginBottom: '10px' }}>日间记忆分析</h3>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <input type="number" min={0} max={23} value={dayHour} onChange={e => setDayHour(Number(e.target.value))} style={timeInputStyle} />
            <span>:</span>
            <input type="number" min={0} max={59} value={dayMinute} onChange={e => setDayMinute(Number(e.target.value))} style={timeInputStyle} />
          </div>
        </div>

        <div>
          <h3 style={{ fontSize: '15px', marginBottom: '10px' }}>晚间记忆分析</h3>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <input type="number" min={0} max={23} value={nightHour} onChange={e => setNightHour(Number(e.target.value))} style={timeInputStyle} />
            <span>:</span>
            <input type="number" min={0} max={59} value={nightMinute} onChange={e => setNightMinute(Number(e.target.value))} style={timeInputStyle} />
          </div>
        </div>

        <div>
          <h3 style={{ fontSize: '15px', marginBottom: '10px' }}>凌晨整理线程</h3>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <input type="number" min={0} max={23} value={cleanupHour} onChange={e => setCleanupHour(Number(e.target.value))} style={timeInputStyle} />
            <span>:</span>
            <input type="number" min={0} max={59} value={cleanupMinute} onChange={e => setCleanupMinute(Number(e.target.value))} style={timeInputStyle} />
          </div>
        </div>
      </div>

      <div style={{ marginTop: '24px' }}>
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
          {saved ? '已保存 ✓' : '保存配置'}
        </button>
      </div>
    </div>
  );
};

export default MemorySchedule;
