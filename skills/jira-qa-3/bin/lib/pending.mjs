/**
 * 阶段二待确认项计数。
 *
 * total = 门禁未关闭项 + 不可读/被截断材料 + 门禁过期 + XMind【待确认】叶子
 * 同一原因只计一次；评论只输出 total，明细在测试方案与 XMind 中。
 */

import { IGNORED_ROLES } from './material-set.mjs';

export function countPending({ gate, materials, leaveTitles = [] } = {}) {
  const gateCount = Number(gate?.openCount || 0);
  const staleCount = gate?.stale ? 1 : 0;

  const unreadableKeys = new Set();
  for (const material of materials?.materials || []) {
    if (!material) continue;
    if (material.status && material.status !== 'active') continue;
    if (IGNORED_ROLES.has(material.role)) continue;
    if (material.unreadable || material.truncated) {
      unreadableKeys.add(String(material.key || material.id || material.filename || ''));
    }
  }

  const leaves = (leaveTitles || []).filter((title) => String(title || '').startsWith('【待确认】')).length;
  return {
    total: gateCount + staleCount + unreadableKeys.size + leaves,
    gate: gateCount,
    stale: staleCount,
    material: unreadableKeys.size,
    leaves,
  };
}
