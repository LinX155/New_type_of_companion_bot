import React, { useEffect, useState } from 'react';

interface CategoryInfo {
  id: string;
  name: string;
  description: string;
  count: number;
}

interface MemeStats {
  total: number;
  categories: Record<string, number>;
  data_dir: string;
}

interface DraggedMeme {
  category: string;
  stem: string;
}

interface ReclassifyJob {
  job_id: string;
  from_category: string;
  file_stem: string;
  target_category: string;
  status: string;
  queue_position?: number;
  result?: {
    status?: string;
    file_stem?: string;
    target_category?: string;
    error?: string | null;
  } | null;
  error?: string | null;
}

const API_BASE = '';

const terminalJobStatuses = new Set(['completed', 'failed']);

const MemeManager: React.FC = () => {
  const [categories, setCategories] = useState<CategoryInfo[]>([]);
  const [stats, setStats] = useState<MemeStats | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [images, setImages] = useState<string[]>([]);
  const [filteredCount, setFilteredCount] = useState(0);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const [draggedMeme, setDraggedMeme] = useState<DraggedMeme | null>(null);
  const [dragOverCategory, setDragOverCategory] = useState<string | null>(null);
  const [reclassifyJobs, setReclassifyJobs] = useState<Record<string, ReclassifyJob>>({});

  useEffect(() => {
    void fetchCategories();
    void fetchStats();
    void fetchFiltered();
  }, []);

  const fetchCategories = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/memes/categories`);
      if (res.ok) setCategories(await res.json() as CategoryInfo[]);
    } catch {}
  };

  const fetchStats = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/memes/stats`);
      if (res.ok) setStats(await res.json() as MemeStats);
    } catch {}
  };

  const fetchFiltered = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/memes/filtered`);
      if (res.ok) {
        const data = await res.json() as { count?: number };
        setFilteredCount(data.count || 0);
      }
    } catch {}
  };

  const selectCategory = async (catId: string) => {
    setSelectedCategory(catId);
    try {
      const res = await fetch(`${API_BASE}/api/memes/category/${catId}`);
      if (!res.ok) {
        setImages([]);
        return;
      }
      const data = await res.json() as { images?: string[] };
      setImages(data.images || []);
    } catch {
      setImages([]);
    }
  };

  const refreshMemeLists = async () => {
    await Promise.all([fetchCategories(), fetchStats(), fetchFiltered()]);
    if (selectedCategory) await selectCategory(selectedCategory);
  };

  const deleteImage = async (fileStem: string) => {
    if (!confirm(`确认删除 ${fileStem}？`)) return;
    const catId = selectedCategory;
    if (!catId) return;
    try {
      const res = await fetch(`${API_BASE}/api/memes/image/${catId}/${fileStem}`, { method: 'DELETE' });
      if (res.ok) {
        setImages(prev => prev.filter(i => i !== fileStem));
        await Promise.all([fetchStats(), fetchCategories()]);
      }
    } catch {}
  };

  const copyMemeId = (fileStem: string) => {
    void navigator.clipboard.writeText(`meme:${fileStem}`);
  };

  const jobKey = (category: string, stem: string) => `${category}:${stem}`;

  const isJobActive = (job?: ReclassifyJob) => Boolean(job && !terminalJobStatuses.has(job.status));

  const parseDraggedMeme = (event: React.DragEvent): DraggedMeme | null => {
    if (draggedMeme) return draggedMeme;
    const raw = event.dataTransfer.getData('application/json') || event.dataTransfer.getData('text/plain');
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw) as DraggedMeme;
      if (parsed.category && parsed.stem) return parsed;
    } catch {}
    return null;
  };

  const pollReclassifyJob = (jobId: string, key: string) => {
    const poll = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/memes/reclassify/jobs/${encodeURIComponent(jobId)}`);
        if (!res.ok) throw new Error('poll_failed');
        const job = await res.json() as ReclassifyJob;
        setReclassifyJobs(prev => ({ ...prev, [key]: job }));
        if (terminalJobStatuses.has(job.status)) {
          await refreshMemeLists();
          return;
        }
        window.setTimeout(poll, 1000);
      } catch {
        setReclassifyJobs(prev => ({
          ...prev,
          [key]: {
            ...(prev[key] || {
              job_id: jobId,
              from_category: '',
              file_stem: '',
              target_category: '',
            }),
            status: 'failed',
            error: 'poll_failed',
          },
        }));
      }
    };
    window.setTimeout(poll, 700);
  };

  const requestReclassify = async (targetCategory: string, item: DraggedMeme | null) => {
    setDragOverCategory(null);
    setDraggedMeme(null);
    if (!item || item.category === targetCategory) return;

    const key = jobKey(item.category, item.stem);
    const optimisticJob: ReclassifyJob = {
      job_id: '',
      from_category: item.category,
      file_stem: item.stem,
      target_category: targetCategory,
      status: 'queued',
    };
    setReclassifyJobs(prev => ({ ...prev, [key]: optimisticJob }));

    try {
      const res = await fetch(`${API_BASE}/api/memes/reclassify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          from_category: item.category,
          file_stem: item.stem,
          target_category: targetCategory,
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || 'request_failed');
      }
      const data = await res.json() as { job: ReclassifyJob };
      setReclassifyJobs(prev => ({ ...prev, [key]: data.job }));
      pollReclassifyJob(data.job.job_id, key);
    } catch (error) {
      setReclassifyJobs(prev => ({
        ...prev,
        [key]: {
          ...optimisticJob,
          status: 'failed',
          error: error instanceof Error ? error.message : 'request_failed',
        },
      }));
    }
  };

  const activeJobCount = Object.values(reclassifyJobs).filter(job => isJobActive(job)).length;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      <div style={{ background: '#fff', padding: '16px', borderRadius: '8px', display: 'flex', gap: '24px', flexWrap: 'wrap' }}>
        <div><strong>表情总数:</strong> {stats?.total ?? '-'}</div>
        <div><strong>表情目录:</strong> {stats?.data_dir ?? '-'}</div>
        <div><strong>过滤记录:</strong> {filteredCount}</div>
        <div><strong>重分类队列:</strong> {activeJobCount}</div>
      </div>

      <div style={{ display: 'flex', gap: '16px', flex: 1, minHeight: 0 }}>
        <div style={{ width: '260px', background: '#fff', borderRadius: '8px', padding: '16px' }}>
          <h3 style={{ marginBottom: '12px', fontSize: '15px' }}>分类列表</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {categories.map(cat => {
              const canDrop = Boolean(draggedMeme && draggedMeme.category !== cat.id);
              const isDragOver = dragOverCategory === cat.id && canDrop;
              return (
                <button
                  key={cat.id}
                  onClick={() => void selectCategory(cat.id)}
                  onDragOver={event => {
                    if (!canDrop) return;
                    event.preventDefault();
                    event.dataTransfer.dropEffect = 'move';
                  }}
                  onDragEnter={() => {
                    if (canDrop) setDragOverCategory(cat.id);
                  }}
                  onDragLeave={() => {
                    if (dragOverCategory === cat.id) setDragOverCategory(null);
                  }}
                  onDrop={event => {
                    event.preventDefault();
                    void requestReclassify(cat.id, parseDraggedMeme(event));
                  }}
                  style={{
                    textAlign: 'left',
                    padding: '10px 12px',
                    border: isDragOver ? '1px solid #2ecc71' : '1px solid #eee',
                    borderRadius: '6px',
                    background: isDragOver ? '#ecfff4' : selectedCategory === cat.id ? '#3498db' : '#fff',
                    color: selectedCategory === cat.id && !isDragOver ? '#fff' : '#2c3e50',
                    cursor: 'pointer',
                    fontSize: '13px',
                  }}
                >
                  <div style={{ fontWeight: 600 }}>{cat.name} ({cat.count})</div>
                  <div style={{ fontSize: '11px', opacity: 0.8, marginTop: '2px' }}>{cat.description}</div>
                </button>
              );
            })}
          </div>
        </div>

        <div style={{ flex: 1, background: '#fff', borderRadius: '8px', padding: '16px', minWidth: 0 }}>
          {selectedCategory ? (
            <>
              <div style={{ marginBottom: '12px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h3 style={{ fontSize: '15px' }}>
                  {categories.find(c => c.id === selectedCategory)?.name} - {images.length} 张
                </h3>
              </div>
              {images.length === 0 ? (
                <div style={{ color: '#95a5a6', textAlign: 'center', padding: '40px' }}>暂无表情</div>
              ) : (
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))', gap: '12px' }}>
                  {images.map(stem => {
                    const key = jobKey(selectedCategory, stem);
                    const job = reclassifyJobs[key];
                    const busy = isJobActive(job);
                    const failed = job?.status === 'failed';
                    return (
                      <div
                        key={stem}
                        draggable={!busy}
                        onDragStart={event => {
                          const payload = { category: selectedCategory, stem };
                          setDraggedMeme(payload);
                          event.dataTransfer.effectAllowed = 'move';
                          event.dataTransfer.setData('application/json', JSON.stringify(payload));
                        }}
                        onDragEnd={() => {
                          setDraggedMeme(null);
                          setDragOverCategory(null);
                        }}
                        style={{
                          border: failed ? '1px solid #e74c3c' : '1px solid #eee',
                          borderRadius: '8px',
                          overflow: 'hidden',
                          position: 'relative',
                          opacity: busy ? 0.65 : 1,
                          cursor: busy ? 'wait' : 'grab',
                        }}
                      >
                        {job && (
                          <div style={{
                            position: 'absolute',
                            top: '6px',
                            right: '6px',
                            zIndex: 1,
                            fontSize: '11px',
                            padding: '2px 6px',
                            borderRadius: '999px',
                            background: failed ? '#e74c3c' : busy ? '#f39c12' : '#2ecc71',
                            color: '#fff',
                          }}>
                            {failed ? '失败' : busy ? '处理中' : '完成'}
                          </div>
                        )}
                        <img
                          src={`${API_BASE}/api/memes/image/${selectedCategory}/${stem}`}
                          alt={stem}
                          style={{ width: '100%', height: '120px', objectFit: 'cover', cursor: 'pointer' }}
                          onClick={() => setPreviewImage(`${API_BASE}/api/memes/image/${selectedCategory}/${stem}`)}
                        />
                        <div style={{ padding: '6px 8px', fontSize: '11px', wordBreak: 'break-all', color: '#555', minHeight: '34px' }}>
                          {stem}
                        </div>
                        <div style={{ display: 'flex', gap: '4px', padding: '0 8px 8px' }}>
                          <button
                            onClick={() => copyMemeId(stem)}
                            disabled={busy}
                            style={{ flex: 1, padding: '4px', fontSize: '11px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: busy ? 'not-allowed' : 'pointer' }}
                          >
                            复制ID
                          </button>
                          <button
                            onClick={() => void deleteImage(stem)}
                            disabled={busy}
                            style={{ flex: 1, padding: '4px', fontSize: '11px', border: '1px solid #e74c3c', borderRadius: '4px', background: '#fff', color: '#e74c3c', cursor: busy ? 'not-allowed' : 'pointer' }}
                          >
                            删除
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          ) : (
            <div style={{ color: '#95a5a6', textAlign: 'center', padding: '40px' }}>请选择一个分类</div>
          )}
        </div>
      </div>

      {previewImage && (
        <div
          onClick={() => setPreviewImage(null)}
          style={{
            position: 'fixed',
            top: 0, left: 0, right: 0, bottom: 0,
            background: 'rgba(0,0,0,0.7)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
          }}
        >
          <img
            src={previewImage}
            alt="preview"
            style={{ maxWidth: '80%', maxHeight: '80%', borderRadius: '8px' }}
            onClick={event => event.stopPropagation()}
          />
        </div>
      )}
    </div>
  );
};

export default MemeManager;
