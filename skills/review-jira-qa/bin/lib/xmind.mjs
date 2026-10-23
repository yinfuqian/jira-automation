/**
 * XMind 2.0 读写（ZIP + content.xml）。
 * 只覆盖本技能需要的子集：sheet → root topic → children、notes、priority marker。
 */

import { readZip, writeZip } from './zip.mjs';
import { EXIT, fail } from './env.mjs';

const NS = 'urn:xmind:xmap:xmlns:content:2.0';

export function escapeXml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

export function decodeXml(value) {
  return String(value ?? '')
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, dec) => String.fromCodePoint(Number(dec)))
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}

function topicXml(node, state) {
  const id = `topic-${state.next++}`;
  const title = `<title>${escapeXml(node.title || '')}</title>`;
  const notes = node.notes ? `<notes><plain>${escapeXml(node.notes)}</plain></notes>` : '';
  const marker =
    node.priority && /^priority-[1-4]$/.test(node.priority)
      ? `<marker-refs><marker-ref marker-id="${node.priority}"/></marker-refs>`
      : '';
  const children = Array.isArray(node.children) && node.children.length
    ? `<children><topics type="attached">${node.children.map((c) => topicXml(c, state)).join('')}</topics></children>`
    : '';
  return `<topic id="${id}">${title}${notes}${marker}${children}</topic>`;
}

export function buildXMind(spec) {
  if (!spec || !spec.root || !spec.root.title) fail('xmind.spec.json 缺少 root.title。', EXIT.VALIDATION);
  const state = { next: 1 };
  const content = `<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<xmap-content xmlns="${NS}" xmlns:fo="http://www.w3.org/1999/XSL/Format" xmlns:svg="http://www.w3.org/2000/svg" xmlns:xhtml="http://www.w3.org/1999/xhtml" xmlns:xlink="http://www.w3.org/1999/xlink" version="2.0">
<sheet id="sheet-1">${topicXml(spec.root, state)}<title>${escapeXml(spec.sheetTitle || spec.root.title)}</title></sheet>
</xmap-content>
`;
  const manifest = `<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<manifest xmlns="urn:xmind:xmap:xmlns:manifest:1.0">
<file-entry full-path="content.xml" media-type="text/xml"/>
<file-entry full-path="META-INF/" media-type=""/>
<file-entry full-path="META-INF/manifest.xml" media-type="text/xml"/>
<file-entry full-path="meta.xml" media-type="text/xml"/>
<file-entry full-path="styles.xml" media-type="text/xml"/>
</manifest>
`;
  const meta = `<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<meta xmlns="urn:xmind:xmap:xmlns:meta:2.0" version="2.0"/>
`;
  const styles = `<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<xmap-styles xmlns="urn:xmind:xmap:xmlns:style:2.0" xmlns:fo="http://www.w3.org/1999/XSL/Format" version="2.0"/>
`;
  return writeZip({
    'content.xml': content,
    'META-INF/manifest.xml': manifest,
    'meta.xml': meta,
    'styles.xml': styles,
  });
}

export function parseXMind(buffer) {
  const entries = readZip(buffer);
  const content = entries.get('content.xml');
  if (!content) fail('XMind 缺少 content.xml。', EXIT.VALIDATION);
  const xml = content.toString('utf8');
  if (!xml.includes(NS)) fail('XMind content.xml 未使用 XMind 2.0 默认命名空间。', EXIT.VALIDATION);

  const root = { title: '', notes: '', priority: '', children: [] };
  const stack = [root];
  let inNotes = false;
  const tokenRe = /<topic\b[^>]*>|<\/topic>|<title>([\s\S]*?)<\/title>|<notes>|<\/notes>|<plain>([\s\S]*?)<\/plain>|<marker-ref\b[^>]*marker-id="([^"]+)"[^>]*\/?>/g;
  for (const m of xml.matchAll(tokenRe)) {
    const token = m[0];
    if (token.startsWith('<topic')) {
      const node = { title: '', notes: '', priority: '', children: [] };
      stack[stack.length - 1].children.push(node);
      stack.push(node);
    } else if (token === '</topic>') {
      if (stack.length > 1) stack.pop();
    } else if (m[1] !== undefined) {
      stack[stack.length - 1].title = decodeXml(m[1].replace(/<[^>]+>/g, '')).trim();
    } else if (token === '<notes>') {
      inNotes = true;
    } else if (token === '</notes>') {
      inNotes = false;
    } else if (m[2] !== undefined) {
      if (inNotes) stack[stack.length - 1].notes = decodeXml(m[2].replace(/<[^>]+>/g, '')).trim();
    } else if (m[3] !== undefined) {
      stack[stack.length - 1].priority = m[3];
    }
  }
  // 第一个 topic 是根；栈底 root 只是哨兵
  return root.children[0] || root;
}

export function walkLeaves(root) {
  const leaves = [];
  const visit = (node, ancestors) => {
    const kids = node.children || [];
    const pathTitles = [...ancestors, node.title].filter(Boolean);
    if (kids.length === 0) {
      leaves.push({ ...node, ancestors: pathTitles.slice(0, -1), path: pathTitles });
    } else {
      for (const child of kids) visit(child, pathTitles);
    }
  };
  visit(root, []);
  return leaves;
}

export function countTopics(root) {
  let count = 0;
  const visit = (node) => {
    count += 1;
    for (const child of node.children || []) visit(child);
  };
  visit(root);
  return count;
}
