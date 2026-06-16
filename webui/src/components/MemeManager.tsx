import React, { useState, useEffect } from 'react';

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

const API_BASE = '';

const MemeManager: React.FC = () => {
  const [categories, setCategories] = useState<CategoryInfo[]>([]);
  const [stats, setStats] = useState<MemeStats | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [images, setImages] = useState<string[]>([]);
  const [filteredCount, setFilteredCount] = useState(0);
  const [previewImage, setPreviewImage] = useState<string | null>(null);

  useEffect(() => {
    fetchCategories();
    fetchStats();
    fetchFiltered();
  }, []);

  const fetchCategories = () => {
    fetch(`${API_BASE}/api/memes/categories`)
      .then(r => r.json())
      .then(data => setCategories(data))
      .catch(() => {});
  };

  const fetchStats = () => {
    fetch(`${API_BASE}/api/memes/stats`)
      .then(r => r.json())
      .then(data => setStats(data))
      .catch(() => {});
  };

  const fetchFiltered = () => {
    fetch(`${API_BASE}/api/memes/filtered`)
      .then(r => r.json())
      .then(data => setFilteredCount(data.count || 0))
      .catch(() => {});
  };

  const selectCategory = (catId: string) => {
    setSelectedCategory(catId);
    fetch(`${API_BASE}/api/memes/category/${catId}`)
      .then(r => r.json())
      .then(data => setImages(data.images || []))
      .catch(() => setImages([]));
  };

  const deleteImage = async (fileStem: string) => {
    if (!confirm(`确定要删除 ${fileStem} 吗？`)) return;
    const catId = selectedCategory;
    if (!catId) return;
    try {
      const res = await fetch(`${API_BASE}/api/memes/image/${catId}/${fileStem}`, { method: 'DELETE' });
      if (res.ok) {
        setImages(prev => prev.filter(i => i !== fileStem));
        fetchStats();
        fetchCategories();
      }
    } catch {}
  };

  const copyMemeId = (fileStem: string) => {
    navigator.clipboard.writeText(`meme:${fileStem}`);
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
      {/* Stats bar */}
      <div style={{ background: '#fff', padding: '16px', borderRadius: '8px', display: 'flex', gap: '24px', flexWrap: 'wrap' }}>
        <div><strong>表情总数:</strong> {stats?.total ?? '-'}</div>
        <div><strong>数据目录:</strong> {stats?.data_dir ?? '-'}</div>
        <div><strong>过滤记录:</strong> {filteredCount}</div>
      </div>

      <div style={{ display: 'flex', gap: '16px', flex: 1 }}>
        {/* Category list */}
        <div style={{ width: '260px', background: '#fff', borderRadius: '8px', padding: '16px' }}>
          <h3 style={{ marginBottom: '12px', fontSize: '15px' }}>分类列表</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            {categories.map(cat => (
              <button
                key={cat.id}
                onClick={() => selectCategory(cat.id)}
                style={{
                  textAlign: 'left',
                  padding: '10px 12px',
                  border: '1px solid #eee',
                  borderRadius: '6px',
                  background: selectedCategory === cat.id ? '#3498db' : '#fff',
                  color: selectedCategory === cat.id ? '#fff' : '#2c3e50',
                  cursor: 'pointer',
                  fontSize: '13px',
                }}
              >
                <div style={{ fontWeight: 600 }}>{cat.name} ({cat.count})</div>
                <div style={{ fontSize: '11px', opacity: 0.8, marginTop: '2px' }}>{cat.description}</div>
              </button>
            ))}
          </div>
        </div>

        {/* Image grid */}
        <div style={{ flex: 1, background: '#fff', borderRadius: '8px', padding: '16px' }}>
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
                  {images.map(stem => (
                    <div key={stem} style={{ border: '1px solid #eee', borderRadius: '8px', overflow: 'hidden', position: 'relative' }}>
                      <img
                        src={`${API_BASE}/api/memes/image/${selectedCategory}/${stem}`}
                        alt={stem}
                        style={{ width: '100%', height: '120px', objectFit: 'cover', cursor: 'pointer' }}
                        onClick={() => setPreviewImage(`${API_BASE}/api/memes/image/${selectedCategory}/${stem}`)}
                      />
                      <div style={{ padding: '6px 8px', fontSize: '11px', wordBreak: 'break-all', color: '#555' }}>
                        {stem}
                      </div>
                      <div style={{ display: 'flex', gap: '4px', padding: '0 8px 8px' }}>
                        <button
                          onClick={() => copyMemeId(stem)}
                          style={{ flex: 1, padding: '4px', fontSize: '11px', border: '1px solid #ddd', borderRadius: '4px', background: '#fff', cursor: 'pointer' }}
                        >
                          复制ID
                        </button>
                        <button
                          onClick={() => deleteImage(stem)}
                          style={{ flex: 1, padding: '4px', fontSize: '11px', border: '1px solid #e74c3c', borderRadius: '4px', background: '#fff', color: '#e74c3c', cursor: 'pointer' }}
                        >
                          删除
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div style={{ color: '#95a5a6', textAlign: 'center', padding: '40px' }}>请选择一个分类</div>
          )}
        </div>
      </div>

      {/* Preview modal */}
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
            onClick={e => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  );
};

export default MemeManager;
