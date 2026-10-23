/**
 * 从常见需求材料中抽取可读文本。
 * PDF/图片不抽取，交由模型用 Read 工具查看。
 */

import path from 'node:path';

import { readZip } from './zip.mjs';

const TEXT_EXT = new Set(['.md', '.markdown', '.txt', '.json', '.csv', '.yaml', '.yml', '.xml']);

export function decodeEntities(text) {
  return String(text)
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, dec) => String.fromCodePoint(Number(dec)))
    .replace(/&nbsp;/gi, ' ')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&amp;/gi, '&');
}

export function htmlToText(raw) {
  const withoutScripts = String(raw).replace(/<script[\s\S]*?<\/script>/gi, ' ');
  const withoutStyles = withoutScripts.replace(/<style[\s\S]*?<\/style>/gi, ' ');
  const text = withoutStyles
    .replace(/<\/(p|div|li|tr|h[1-6]|section|table|thead|tbody)>/gi, '\n')
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/t[dh]>/gi, ' | ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .replace(/\n\s*\n+/g, '\n')
    .trim();
  return decodeEntities(text);
}

export function scriptStrings(raw) {
  const strings = [];
  for (const match of String(raw).matchAll(/<script[^>]*>([\s\S]*?)<\/script>/gi)) {
    const body = match[1];
    for (const m of body.matchAll(/[一-龥][^\n'"`]{2,}/g)) {
      const value = m[0].trim();
      if (value.length >= 3 && !strings.includes(value)) strings.push(value);
    }
  }
  return strings;
}

function xmlText(xml, tagPattern = '<t[^>]*>([\\s\\S]*?)</t>') {
  const values = [];
  for (const m of String(xml).matchAll(new RegExp(tagPattern, 'gi'))) {
    const value = decodeEntities(m[1].replace(/<[^>]+>/g, '')).trim();
    if (value) values.push(value);
  }
  return values.join('\n');
}

export function extractText(filename, buffer) {
  const ext = path.extname(String(filename)).toLowerCase();
  if (TEXT_EXT.has(ext)) {
    return { kind: 'text', text: buffer.toString('utf8'), needsRead: false };
  }
  if (ext === '.html' || ext === '.htm') {
    const visible = htmlToText(buffer.toString('utf8'));
    const scripts = scriptStrings(buffer.toString('utf8'));
    const text = scripts.length ? `${visible}\n\n[脚本内正文]\n${scripts.join('\n')}` : visible;
    return { kind: 'html', text, needsRead: false };
  }
  if (ext === '.docx') {
    const entries = readZip(buffer);
    const doc = entries.get('word/document.xml');
    return { kind: 'docx', text: doc ? xmlText(doc.toString('utf8')) : '', needsRead: !doc };
  }
  if (ext === '.xlsx') {
    const entries = readZip(buffer);
    const shared = entries.get('xl/sharedStrings.xml');
    const parts = [];
    if (shared) parts.push(xmlText(shared.toString('utf8')));
    for (const [name, value] of entries) {
      if (/^xl\/worksheets\/sheet\d+\.xml$/.test(name)) {
        parts.push(xmlText(value.toString('utf8'), '<t[^>]*>([\\s\\S]*?)</t>'));
      }
    }
    return { kind: 'xlsx', text: parts.filter(Boolean).join('\n'), needsRead: false };
  }
  if (ext === '.pptx') {
    const entries = readZip(buffer);
    const parts = [];
    for (const [name, value] of entries) {
      if (/^ppt\/slides\/slide\d+\.xml$/.test(name)) {
        parts.push(xmlText(value.toString('utf8'), '<a:t[^>]*>([\\s\\S]*?)</a:t>'));
      }
    }
    return { kind: 'pptx', text: parts.filter(Boolean).join('\n'), needsRead: false };
  }
  if (['.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp'].includes(ext)) {
    return { kind: ext.slice(1), text: '', needsRead: true };
  }
  return { kind: 'binary', text: '', needsRead: true };
}
