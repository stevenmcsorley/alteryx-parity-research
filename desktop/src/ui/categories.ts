// Original colour coding and glyphs per catalogue category (no vendor artwork).
const COLORS: Record<string, string> = {
  'In/Out': '#2f8f5b', Preparation: '#3a6fd8', Join: '#8a52c9', Parse: '#c45d2c', Transform: '#b8860b',
  'In-Database': '#5b6b7f', Reporting: '#c0397a', Documentation: '#7d8796', Spatial: '#1f9aa3', Interface: '#9b6b3c',
  'Data Investigation': '#4a7fa8', Predictive: '#6d4cc2', 'AB Testing': '#7a5bb5', 'Time Series': '#5a5fc4',
  'Predictive Grouping': '#6a55b8', Prescriptive: '#584bb0', Connectors: '#3e8e8e', Address: '#a0703a',
  'Demographic Analysis': '#9a6c4a', 'Behavior Analysis (Deprecated)': '#8b7d6b', Calgary: '#957351',
  Developer: '#4b5563', Laboratory: '#b04848', 'Machine Learning': '#2a7bbd', 'Text Mining': '#7f5aa2',
  'Computer Vision': '#3c8c6a', 'AI Tools': '#c25b3a', Connect: '#3f7f9c', 'SDK Examples': '#6b7280',
};

export function categoryColor(category: string | undefined): string {
  return (category && COLORS[category]) || '#6b7280';
}

export function glyph(name: string): string {
  const words = name.replace(/[^A-Za-z0-9 ]/g, ' ').split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (words[0][0] + words[1][0]).toUpperCase();
  return (words[0] ?? '?').slice(0, 2).toUpperCase();
}
