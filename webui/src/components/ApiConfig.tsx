import React, { useState, useEffect } from 'react';

const API_BASE = '';
const TEMPERATURE_MIN = 1;
const DEEPSEEK_TEMPERATURE_MAX = 2;
const MIMO_TEMPERATURE_MAX = 1.5;

const getTemperatureMax = (baseUrl: string, model: string) => {
  const identity = `${baseUrl || ''} ${model || ''}`.toLowerCase();
  if (identity.includes('xiaomimimo') || identity.includes('mimo')) return MIMO_TEMPERATURE_MAX;
  if (identity.includes('deepseek')) return DEEPSEEK_TEMPERATURE_MAX;
  return DEEPSEEK_TEMPERATURE_MAX;
};

const clampTemperature = (value: number, max: number) => {
  if (!Number.isFinite(value)) return TEMPERATURE_MIN;
  return Math.max(TEMPERATURE_MIN, Math.min(max, value));
};

const ApiConfig: React.FC = () => {
  const [apiKey, setApiKey] = useState('');
  const [baseUrl, setBaseUrl] = useState('https://api.deepseek.com');
  const [model, setModel] = useState('deepseek-v4-flash');
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [temperature, setTemperature] = useState(TEMPERATURE_MIN);
  const [savedApi, setSavedApi] = useState(false);
  const [dirty, setDirty] = useState(false);
  const temperatureMax = getTemperatureMax(baseUrl, model);

  useEffect(() => {
    fetch(`${API_BASE}/api/config`)
      .then(r => r.json())
      .then(data => {
        if (data.api_key) setApiKey(data.api_key);
        if (data.base_url) setBaseUrl(data.base_url);
        if (data.model) setModel(data.model);
        if (data.thinking_enabled !== undefined) setThinkingEnabled(data.thinking_enabled);
        if (data.temperature !== undefined) setTemperature(Number(data.temperature) || TEMPERATURE_MIN);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    setTemperature(prev => clampTemperature(prev, temperatureMax));
  }, [temperatureMax]);

  const saveConfig = async (thinkingValue: boolean, temperatureValue: number = temperature) => {
    const response = await fetch(`${API_BASE}/api/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        api_key: apiKey,
        base_url: baseUrl,
        model,
        thinking_enabled: thinkingValue,
        temperature: clampTemperature(temperatureValue, temperatureMax),
      }),
    });
    const data = await response.json().catch(() => null);
    if (data?.temperature !== undefined) {
      setTemperature(Number(data.temperature) || TEMPERATURE_MIN);
    }
  };

  const handleSave = async () => {
    try {
      await saveConfig(thinkingEnabled);
      setSavedApi(true);
      setDirty(false);
      setTimeout(() => setSavedApi(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  const handleToggleThinking = async (next: boolean) => {
    setThinkingEnabled(next);
    try {
      await saveConfig(next, temperature);
      setSavedApi(true);
      setTimeout(() => setSavedApi(false), 2000);
    } catch {
      alert('保存失败');
    }
  };

  return (
    <div style={{ maxWidth: '600px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      <div style={{ background: '#fff', padding: '24px', borderRadius: '8px' }}>
        <h2 style={{ marginBottom: '20px' }}>大模型 API 配置</h2>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <div>
            <label style={{ display: 'block', marginBottom: '6px', fontWeight: 500 }}>API Key</label>
            <input
              type="text"
              value={apiKey}
              onChange={e => { setApiKey(e.target.value); setDirty(true); }}
              placeholder="输入 API Key..."
              style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '6px', fontSize: '14px' }}
            />
          </div>
          <div>
            <label style={{ display: 'block', marginBottom: '6px', fontWeight: 500 }}>Base URL</label>
            <input
              type="text"
              value={baseUrl}
              onChange={e => { setBaseUrl(e.target.value); setDirty(true); }}
              style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '6px', fontSize: '14px' }}
            />
          </div>
          <div>
            <label style={{ display: 'block', marginBottom: '6px', fontWeight: 500 }}>Model</label>
            <input
              type="text"
              value={model}
              onChange={e => { setModel(e.target.value); setDirty(true); }}
              style={{ width: '100%', padding: '10px', border: '1px solid #ddd', borderRadius: '6px', fontSize: '14px' }}
            />
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px' }}>
              <label style={{ fontWeight: 500 }}>Temperature</label>
              <span style={{ color: '#2c3e50', fontVariantNumeric: 'tabular-nums', fontSize: '14px' }}>
                {temperature.toFixed(2)}
              </span>
            </div>
            <input
              type="range"
              min={TEMPERATURE_MIN}
              max={temperatureMax}
              step={0.05}
              value={clampTemperature(temperature, temperatureMax)}
              onChange={e => {
                setTemperature(clampTemperature(Number(e.target.value), temperatureMax));
                setDirty(true);
              }}
              style={{ width: '100%' }}
            />
            <div style={{ display: 'flex', justifyContent: 'space-between', color: '#7f8c8d', fontSize: '12px' }}>
              <span>最低 {TEMPERATURE_MIN.toFixed(0)}</span>
              <span>{temperatureMax === MIMO_TEMPERATURE_MAX ? 'MiMo' : 'DeepSeek/默认'} 最高 {temperatureMax.toFixed(1)}</span>
            </div>
            <div style={{ color: thinkingEnabled ? '#b26a00' : '#7f8c8d', fontSize: '12px' }}>
              {thinkingEnabled
                ? '思考模式下部分模型会忽略自定义 temperature，后端会优先保证 API 不报错。'
                : '影响主聊天完整链路；看图、记忆整理、存表情等后台任务仍使用低温。'}
            </div>
          </div>

          <div style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '12px',
            background: '#f8f9fa',
            borderRadius: '6px',
          }}>
            <div>
              <div style={{ fontWeight: 500, fontSize: '14px' }}>是否开启思考模式</div>
              <div style={{ color: '#7f8c8d', fontSize: '12px', marginTop: '2px' }}>
                开启后 LLM 会先思考再回复（更慢、更强），关闭则直接快速回复。
              </div>
            </div>
            <label style={{ position: 'relative', display: 'inline-block', width: '44px', height: '24px', flexShrink: 0 }}>
              <input
                type="checkbox"
                checked={thinkingEnabled}
                onChange={e => handleToggleThinking(e.target.checked)}
                style={{ opacity: 0, width: 0, height: 0 }}
              />
              <span style={{
                position: 'absolute',
                cursor: 'pointer',
                top: 0, left: 0, right: 0, bottom: 0,
                background: thinkingEnabled ? '#27ae60' : '#ccc',
                borderRadius: '24px',
                transition: '0.2s',
              }}>
                <span style={{
                  position: 'absolute',
                  height: '18px', width: '18px',
                  left: thinkingEnabled ? '23px' : '3px',
                  bottom: '3px',
                  background: '#fff',
                  borderRadius: '50%',
                  transition: '0.2s',
                }} />
              </span>
            </label>
          </div>

          <button
            onClick={handleSave}
            disabled={!dirty}
            style={{
              padding: '12px',
              background: dirty ? '#3498db' : '#bdc3c7',
              color: '#fff',
              border: 'none',
              borderRadius: '6px',
              cursor: dirty ? 'pointer' : 'not-allowed',
              fontSize: '14px',
              fontWeight: 500,
            }}
          >
            {savedApi ? '已保存 ✓' : '保存配置'}
          </button>
        </div>
      </div>
    </div>
  );
};

export default ApiConfig;
