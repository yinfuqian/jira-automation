/**
 * 包内 WASM 压缩包解码器加载器（libarchive-wasm，vendored）。
 *
 * 硬约束：
 * - 只从技能目录加载，不联网、不依赖系统命令。
 * - 加载失败返回可分类错误，由调用方记为“不可读”，不终止流程。
 */

import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { EXIT, QaError } from './env.mjs';

const require = createRequire(import.meta.url);
const VENDOR_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'vendor', 'libarchive-wasm');
const VENDOR_ENTRY = path.join(VENDOR_DIR, 'index.js');

let vendorPromise = null;
let modulePromise = null;

export function vendorDir() {
  return VENDOR_DIR;
}

export async function loadLibarchiveVendor() {
  if (!vendorPromise) {
    vendorPromise = (async () => {
      try {
        const mod = require(VENDOR_ENTRY);
        if (!mod || typeof mod.ArchiveReader !== 'function' || typeof mod.libarchiveWasm !== 'function') {
          throw new Error('导出不完整');
        }
        return mod;
      } catch (error) {
        throw new QaError(`压缩包解码器加载失败：${error.message}`, EXIT.VALIDATION);
      }
    })();
  }
  return vendorPromise;
}

export async function loadLibarchiveWasm() {
  if (!modulePromise) {
    modulePromise = (async () => {
      const vendor = await loadLibarchiveVendor();
      return vendor.libarchiveWasm();
    })();
  }
  return modulePromise;
}

export async function createArchiveReader(buffer) {
  const vendor = await loadLibarchiveVendor();
  const mod = await loadLibarchiveWasm();
  return { vendor, reader: new vendor.ArchiveReader(mod, new Int8Array(buffer)) };
}
