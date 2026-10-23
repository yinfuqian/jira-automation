/**
 * 最小 ZIP 读写（零依赖），用于 XMind 与 OOXML 文本抽取。
 *
 * 读：中央目录 + 本地头，支持 store / deflate；拒绝加密、路径穿越、超大条目。
 * 写：store / deflate，UTF-8 文件名，固定时间戳，保证可复现。
 */

import { deflateRawSync, inflateRawSync } from 'node:zlib';

import { EXIT, fail } from './env.mjs';

const EOCD = 0x06054b50;
const CENTRAL = 0x02014b50;
const LOCAL = 0x04034b50;

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i += 1) {
    let c = i;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[i] = c >>> 0;
  }
  return table;
})();

export function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function findEocd(buf) {
  for (let i = buf.length - 22; i >= 0 && i >= buf.length - 65558; i -= 1) {
    if (buf.readUInt32LE(i) === EOCD) return i;
  }
  fail('ZIP 结构无效：找不到中央目录结束记录。', EXIT.VALIDATION);
}

export function readZip(buf) {
  if (!Buffer.isBuffer(buf)) fail('ZIP 输入不是 Buffer。', EXIT.VALIDATION);
  const eocd = findEocd(buf);
  const count = buf.readUInt16LE(eocd + 10);
  const cdSize = buf.readUInt32LE(eocd + 12);
  const cdOffset = buf.readUInt32LE(eocd + 16);
  if (cdOffset + cdSize > buf.length) fail('ZIP 中央目录越界。', EXIT.VALIDATION);
  const entries = new Map();
  let offset = cdOffset;
  for (let i = 0; i < count; i += 1) {
    if (buf.readUInt32LE(offset) !== CENTRAL) fail('ZIP 中央目录项损坏。', EXIT.VALIDATION);
    const flags = buf.readUInt16LE(offset + 8);
    const method = buf.readUInt16LE(offset + 10);
    const compSize = buf.readUInt32LE(offset + 20);
    const uncompSize = buf.readUInt32LE(offset + 24);
    const nameLen = buf.readUInt16LE(offset + 28);
    const extraLen = buf.readUInt16LE(offset + 30);
    const commentLen = buf.readUInt16LE(offset + 32);
    const localOffset = buf.readUInt32LE(offset + 42);
    const name = buf.subarray(offset + 46, offset + 46 + nameLen).toString('utf8');
    offset += 46 + nameLen + extraLen + commentLen;
    if (flags & 1) fail(`ZIP 条目已加密，拒绝读取：${name}`, EXIT.VALIDATION);
    if (name.includes('..') || name.startsWith('/') || /^[A-Za-z]:/.test(name)) {
      fail(`ZIP 条目路径非法：${name}`, EXIT.VALIDATION);
    }
    if (uncompSize > 64 * 1024 * 1024) fail(`ZIP 条目过大：${name}`, EXIT.VALIDATION);
    if (buf.readUInt32LE(localOffset) !== LOCAL) fail(`ZIP 本地头损坏：${name}`, EXIT.VALIDATION);
    const localNameLen = buf.readUInt16LE(localOffset + 26);
    const localExtraLen = buf.readUInt16LE(localOffset + 28);
    const dataStart = localOffset + 30 + localNameLen + localExtraLen;
    const data = buf.subarray(dataStart, dataStart + compSize);
    let content;
    if (method === 0) content = Buffer.from(data);
    else if (method === 8) content = inflateRawSync(data);
    else fail(`不支持的 ZIP 压缩方法 ${method}：${name}`, EXIT.VALIDATION);
    if (content.length !== uncompSize) fail(`ZIP 条目大小不符：${name}`, EXIT.VALIDATION);
    entries.set(name, content);
  }
  return entries;
}

export function writeZip(files, { compress = true, date = new Date('2026-01-01T00:00:00Z') } = {}) {
  const chunks = [];
  const central = [];
  let offset = 0;
  const dosTime = ((date.getUTCHours() << 11) | (date.getUTCMinutes() << 5) | (date.getUTCSeconds() >> 1)) & 0xffff;
  const dosDate = (((date.getUTCFullYear() - 1980) << 9) | ((date.getUTCMonth() + 1) << 5) | date.getUTCDate()) & 0xffff;

  for (const [name, value] of Object.entries(files)) {
    const data = Buffer.isBuffer(value) ? value : Buffer.from(String(value), 'utf8');
    const nameBuf = Buffer.from(name, 'utf8');
    const crc = crc32(data);
    const compressed = compress ? deflateRawSync(data) : data;
    const method = compress ? 8 : 0;
    const local = Buffer.alloc(30);
    local.writeUInt32LE(LOCAL, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6);
    local.writeUInt16LE(method, 8);
    local.writeUInt16LE(dosTime, 10);
    local.writeUInt16LE(dosDate, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(compressed.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBuf.length, 26);
    local.writeUInt16LE(0, 28);
    chunks.push(local, nameBuf, compressed);

    const cent = Buffer.alloc(46);
    cent.writeUInt32LE(CENTRAL, 0);
    cent.writeUInt16LE(20, 4);
    cent.writeUInt16LE(20, 6);
    cent.writeUInt16LE(0x0800, 8);
    cent.writeUInt16LE(method, 10);
    cent.writeUInt16LE(dosTime, 12);
    cent.writeUInt16LE(dosDate, 14);
    cent.writeUInt32LE(crc, 16);
    cent.writeUInt32LE(compressed.length, 20);
    cent.writeUInt32LE(data.length, 24);
    cent.writeUInt16LE(nameBuf.length, 28);
    cent.writeUInt32LE(offset, 42);
    central.push(cent, nameBuf);

    offset += local.length + nameBuf.length + compressed.length;
  }

  const centralBuf = Buffer.concat(central);
  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(EOCD, 0);
  eocd.writeUInt16LE(Object.keys(files).length, 8);
  eocd.writeUInt16LE(Object.keys(files).length, 10);
  eocd.writeUInt32LE(centralBuf.length, 12);
  eocd.writeUInt32LE(offset, 16);
  return Buffer.concat([...chunks, centralBuf, eocd]);
}
