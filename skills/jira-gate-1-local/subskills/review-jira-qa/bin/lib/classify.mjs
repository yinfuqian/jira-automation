/**
 * 需求材料分类：QA 交付物 / 评审产物 / 开发设计材料 / 需求侧材料。
 *
 * 规则见 docs/JIRA-RULE.md 与 prompts/stage1-gate.md：
 * - 先排除交付物、评审产物、开发设计材料；
 * - Overview/评审概览 是需求侧材料；
 * - 需求 MD 与 docx/xlsx PRD 是主材料候选；
 * - 其他需求侧材料为 auxiliary/prototype/template。
 */

import path from 'node:path';

const QA_DELIVERABLE_PATTERNS = [
  /(?:^|[-_ ])(?:需求准入门禁清单|测试方案|测试用例|测试点)(?:[-_ ]?v\d+(?:\.\d+)*)?\.(?:html?|md|xmind)$/i,
  /(?:^|[-_ ])Jira导入\.csv$/i,
  /(?:^|[-_ ])xray[^/]*\.csv$/i,
];

const REVIEW_ARTIFACT_PATTERNS = [
  /(需求评审报告|评审报告|准入报告|准入审查|准入审核|设计准入|测试风险分析|风险分析报告|评审记录|评审意见|评审结论|复核记录|复核意见|复核结论)/i,
  /(?:^|[-_. ])(?:comment|review)(?:[-_. ]|$)/i,
];

const TECH_DESIGN_PATTERNS = [/(技术方案|开发方案|技术协议|开发设计)/i];

// 同时含需求/功能描述字样的「技术方案」多为需求正文（如 功能描述与技术方案.md），不得按开发设计材料排除。
const REQUIREMENT_HINT_PATTERN = /(需求|功能描述|产品说明|PRD)/i;

const PRD_NAME_PATTERN = /(PRD|需求文档|需求说明|需求规格|需求清单)/i;
const OVERVIEW_PATTERN = /(Overview|评审概览)/i;
const PROTOTYPE_PATTERN = /(Prototype|原型|设计稿|Design)/i;
const TEMPLATE_PATTERN = /(模板|template)/i;
// 截图类附件：可查看，但不作为需求材料参与材料集合与主材料判定。
const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff']);

export const REQUIREMENT_EXTENSIONS = new Set([
  '.md',
  '.markdown',
  '.txt',
  '.html',
  '.htm',
  '.pdf',
  '.docx',
  '.doc',
  '.xlsx',
  '.xls',
  '.pptx',
  '.csv',
  '.png',
  '.jpg',
  '.jpeg',
  '.rar',
  '.7z',
  '.zip',
]);

export const ARCHIVE_EXTENSIONS = new Set(['.rar', '.7z', '.zip']);

export const ARCHIVE_MEMBER_EXTENSIONS = new Set([
  '.md',
  '.markdown',
  '.txt',
  '.html',
  '.htm',
  '.pdf',
  '.docx',
  '.doc',
  '.xlsx',
  '.xls',
  '.pptx',
  '.csv',
  '.png',
  '.jpg',
  '.jpeg',
]);

export function basenameOf(value) {
  return path.posix.basename(String(value ?? '').replace(/\\/g, '/'));
}

export function extensionOf(value) {
  return path.posix.extname(String(value ?? '').replace(/\\/g, '/')).toLowerCase();
}

export function isRequirementExtension(value) {
  return REQUIREMENT_EXTENSIONS.has(extensionOf(value));
}

export function isArchiveExtension(value) {
  return ARCHIVE_EXTENSIONS.has(extensionOf(value));
}

export function isArchiveMemberExtension(value) {
  return ARCHIVE_MEMBER_EXTENSIONS.has(extensionOf(value));
}

export function isIgnoredPath(value) {
  const raw = String(value ?? '').replace(/\\/g, '/');
  const segments = raw.split('/').filter(Boolean);
  return segments.some(
    (segment) =>
      segment === 'node_modules' ||
      segment === '__MACOSX' ||
      segment === '.git' ||
      segment === 'review' ||
      segment === 'risk-analysis' ||
      segment.startsWith('.') ||
      segment.startsWith('_'),
  );
}

export function isQaDeliverable(filename) {
  const base = basenameOf(filename);
  return QA_DELIVERABLE_PATTERNS.some((re) => re.test(base));
}

export function isReviewArtifact(filename) {
  const base = basenameOf(filename);
  return REVIEW_ARTIFACT_PATTERNS.some((re) => re.test(base));
}

export function isTechDesign(filename) {
  const base = basenameOf(filename);
  return TECH_DESIGN_PATTERNS.some((re) => re.test(base));
}

/**
 * 返回角色：
 * qa-deliverable | qa-generated | excluded | overview | prd-md | prd-doc |
 * prototype | template | auxiliary
 */
export function classifyRole(filename, fullPath = '') {
  const base = basenameOf(fullPath || filename);
  const ext = extensionOf(base);
  if (isQaDeliverable(base)) return 'qa-deliverable';
  if (isReviewArtifact(base)) return 'qa-generated';
  if (isTechDesign(base) && !REQUIREMENT_HINT_PATTERN.test(base)) return 'excluded';
  if (OVERVIEW_PATTERN.test(base)) return 'overview';
  if (ext === '.md' || ext === '.markdown') return 'prd-md';
  if (['.docx', '.xlsx', '.doc', '.xls'].includes(ext) && PRD_NAME_PATTERN.test(base)) return 'prd-doc';
  if (PROTOTYPE_PATTERN.test(base)) return 'prototype';
  if (TEMPLATE_PATTERN.test(base) && ext === '.xlsx') return 'template';
  if (IMAGE_EXTENSIONS.has(ext)) return 'image';
  return 'auxiliary';
}

export function isExcludedRole(role) {
  return role === 'qa-deliverable' || role === 'qa-generated' || role === 'excluded';
}

export function isPrimaryTierRole(role) {
  return role === 'prd-md' || role === 'prd-doc';
}

export function hasPrdName(filename) {
  return PRD_NAME_PATTERN.test(basenameOf(filename));
}
