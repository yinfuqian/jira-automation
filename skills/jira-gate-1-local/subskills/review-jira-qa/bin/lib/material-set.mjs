/**
 * 材料集合口径：门禁、复用、阶段二“材料未变”统一使用。
 *
 * - 只取 active 的顶层附件 ID；
 * - 排除 QA 交付物、评审产物、开发设计材料、superseded、unsupported；
 * - Jira 需求描述用固定键 issue-description 参与比较。
 */

export const IGNORED_ROLES = new Set([
  'qa-deliverable',
  'qa-generated',
  'excluded',
  'superseded',
  'unsupported',
  'image',
]);

export const DESCRIPTION_ID = 'issue-description';

export function isActiveMaterial(material) {
  return Boolean(material) && (material.status === undefined || material.status === 'active');
}

export function countsForMaterialSet(material) {
  if (!isActiveMaterial(material)) return false;
  if (IGNORED_ROLES.has(material.role)) return false;
  return Boolean(material.id);
}

export function effectiveIds(materials) {
  const ids = new Set();
  for (const material of materials?.materials || []) {
    if (!countsForMaterialSet(material)) continue;
    ids.add(String(material.id));
  }
  if ((materials?.materials || []).some((m) => countsForMaterialSet(m) && m.role === 'description')) {
    ids.add(DESCRIPTION_ID);
  }
  return sortIds([...ids]);
}

export function effectiveMaterials(materials) {
  return (materials?.materials || []).filter(countsForMaterialSet);
}

export function sortIds(ids) {
  return [...new Set(ids.map(String))].sort((a, b) => {
    const na = Number(a);
    const nb = Number(b);
    if (Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
    if (Number.isFinite(na)) return -1;
    if (Number.isFinite(nb)) return 1;
    return a.localeCompare(b);
  });
}

export function expandIds(text) {
  const ids = new Set();
  const source = String(text || '');
  for (const match of source.matchAll(/(\d{3,})\s*[–~-]\s*(\d{3,})/g)) {
    const a = Number(match[1]);
    const b = Number(match[2]);
    const lo = Math.min(a, b);
    const hi = Math.max(a, b);
    if (hi - lo <= 200) for (let i = lo; i <= hi; i += 1) ids.add(String(i));
  }
  for (const match of source.matchAll(/(?<![–~-])\b(\d{3,})\b(?![–~-])/g)) ids.add(String(match[1]));
  return [...ids];
}

export function parseMaterialLine(line) {
  const text = String(line || '');
  const hasDescription = /需求描述取自\s*Jira\s*字段/.test(text);
  const withoutDescription = text.replace(/需求描述取自\s*Jira\s*字段/g, '');
  return { ids: expandIds(withoutDescription), hasDescription };
}

export function sameIdSet(a, b, { hasDescriptionA = false, hasDescriptionB = false } = {}) {
  const setA = new Set(sortIds(a));
  const setB = new Set(sortIds(b));
  if (hasDescriptionA !== hasDescriptionB) return false;
  if (setA.size !== setB.size) return false;
  for (const id of setA) if (!setB.has(id)) return false;
  return true;
}
