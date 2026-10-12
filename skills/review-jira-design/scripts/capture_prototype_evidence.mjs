#!/usr/bin/env node
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import net from 'node:net';
import os from 'node:os';

async function loadPlaywright() {
  try {
    return await import('playwright');
  } catch (directError) {
    const packageRoots = [
      process.env.ZY_ADMISSION_NODE_MODULES,
      path.resolve(path.dirname(process.execPath), '..', 'node_modules')
    ].filter(Boolean);
    for (const root of packageRoots) {
      try {
        return await import(pathToFileURL(path.join(root, 'playwright', 'index.js')).href);
      } catch (fallbackError) {
        // Continue to the next configured runtime path.
      }
    }
    throw new Error('找不到 Playwright。请使用带内置浏览器依赖的 Node 运行，或设置 ZY_ADMISSION_NODE_MODULES。');
  }
}

function readArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith('--')) continue;
    const key = token.slice(2);
    args[key] = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
  }
  return args;
}

function usage() {
  console.error('用法：node capture_prototype_evidence.mjs --input <HTML路径或URL> --selector <CSS selector> --out <PNG路径> [--viewport 1440x900]');
}

function toUrl(input) {
  if (/^(https?:|file:)/i.test(input)) return input;
  return pathToFileURL(path.resolve(input)).href;
}

function parseViewport(value) {
  const match = /^([0-9]+)x([0-9]+)$/.exec(String(value || '1440x900'));
  if (!match) throw new Error(`视口格式错误：${value}，应为 WIDTHxHEIGHT`);
  return { width: Number(match[1]), height: Number(match[2]) };
}

function isLocalInput(input) {
  return !/^(https?:|file:)/i.test(input);
}

async function fileExists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

function commandResult(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { windowsHide: true, ...options });
    let stdout = '';
    let stderr = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill();
      reject(new Error(`${command} 执行超时`));
    }, options.timeout ?? 90000);
    child.stdout?.on('data', chunk => { stdout += chunk; });
    child.stderr?.on('data', chunk => { stderr += chunk; });
    child.on('error', error => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
    child.on('close', code => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}

async function findBrowser() {
  const candidates = [
    process.env.ZY_ADMISSION_BROWSER,
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env.ProgramFiles && path.join(process.env.ProgramFiles, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env['ProgramFiles(x86)'] && path.join(process.env['ProgramFiles(x86)'], 'Google', 'Chrome', 'Application', 'chrome.exe'),
    process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    process.env.ProgramFiles && path.join(process.env.ProgramFiles, 'Microsoft', 'Edge', 'Application', 'msedge.exe'),
    process.env['ProgramFiles(x86)'] && path.join(process.env['ProgramFiles(x86)'], 'Microsoft', 'Edge', 'Application', 'msedge.exe')
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (await fileExists(candidate)) return candidate;
  }
  for (const name of ['chrome.exe', 'msedge.exe', 'google-chrome', 'chromium']) {
    const lookup = process.platform === 'win32' ? 'where.exe' : 'which';
    const result = spawnSync(lookup, [name], { encoding: 'utf8', windowsHide: true });
    if (result.status === 0) {
      const found = String(result.stdout || '').split(/\r?\n/).map(item => item.trim()).find(Boolean);
      if (found && await fileExists(found)) return found;
    }
  }
  return null;
}

async function getFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

async function startStaticServer(root) {
  const port = await getFreePort();
  let python = null;
  for (const candidate of ['python', 'py', 'python3']) {
    const probe = spawnSync(candidate, ['--version'], { encoding: 'utf8', windowsHide: true });
    if (!probe.error && probe.status === 0) {
      python = candidate;
      break;
    }
  }
  if (!python) throw new Error('file:// 截图失败，且未找到 Python，无法启动本地 HTTP 服务');
  const child = spawn(python, ['-m', 'http.server', String(port), '--bind', '127.0.0.1', '--directory', root], {
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe']
  });
  await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => resolve(), 1500);
    child.once('error', error => {
      clearTimeout(timer);
      reject(error);
    });
    child.stderr?.on('data', chunk => { output += chunk; });
    child.stdout?.on('data', chunk => { output += chunk; });
  });
  return { child, port };
}

function localHttpUrl(input) {
  const absolute = path.resolve(input);
  const root = path.dirname(absolute);
  const fileName = path.basename(absolute).split(path.sep).map(encodeURIComponent).join('/');
  return { root, path: `/${fileName}` };
}

async function readPngInfo(output) {
  const data = await fs.readFile(output);
  const validSignature = data.length >= 24 && data.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  return {
    validSignature,
    bytes: data.length,
    width: validSignature ? data.readUInt32BE(16) : null,
    height: validSignature ? data.readUInt32BE(20) : null
  };
}

async function inspectCliPage(browser, url, profileDir) {
  const result = await commandResult(browser, [
    '--headless', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
    '--allow-file-access-from-files', '--virtual-time-budget=8000',
    `--user-data-dir=${profileDir}`, '--dump-dom', url
  ], { timeout: 90000 });
  const output = `${result.stdout}\n${result.stderr}`;
  const errorMarkers = ['ErrorResponse', 'ERR_FILE_NOT_FOUND', 'ERR_CONNECTION_REFUSED', 'ERR_FAILED'];
  const marker = errorMarkers.find(item => output.includes(item));
  return { ...result, marker };
}

async function captureWithBrowserCli({ browser, url, output, viewport, selector, input, mode, browserLabel }) {
  const profileDir = await fs.mkdtemp(path.join(os.tmpdir(), 'zy-admission-chrome-'));
  try {
    const screenshot = await commandResult(browser, [
      '--headless', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
      '--allow-file-access-from-files', '--disable-gpu-compositing', '--in-process-gpu',
      `--window-size=${viewport.width},${viewport.height}`,
      '--virtual-time-budget=8000', '--run-all-compositor-stages-before-draw',
      `--user-data-dir=${profileDir}`, `--screenshot=${output}`, url
    ], { timeout: 90000 });
    if (screenshot.code !== 0 || !(await fileExists(output))) {
      throw new Error(`Chrome headless 截图失败：${screenshot.stderr || screenshot.stdout || `退出码 ${screenshot.code}`}`);
    }
    const png = await readPngInfo(output);
    if (!png.validSignature || png.bytes === 0 || png.width !== viewport.width || png.height !== viewport.height) {
      throw new Error(`输出 PNG 无效或尺寸不符：${JSON.stringify(png)}`);
    }
    const dom = await inspectCliPage(browser, url, profileDir);
    if (dom.marker) throw new Error(`页面包含错误标记：${dom.marker}`);
    return {
      ok: true,
      input,
      selector,
      viewport,
      output,
      title: null,
      pageErrors: [],
      captureMode: mode,
      selectorApplied: false,
      browser: browserLabel,
      png,
      note: 'Chrome/Edge CLI 仅截取首屏视口，未应用 selector；请在报告中标记为首屏证据。'
    };
  } finally {
    await fs.rm(profileDir, { recursive: true, force: true }).catch(() => {});
  }
}

const args = readArgs(process.argv.slice(2));
if (!args.input || !args.selector || !args.out) {
  usage();
  process.exitCode = 2;
} else {
  const viewport = parseViewport(args.viewport);
  const output = path.resolve(String(args.out));
  const pageErrors = [];
  let browser;
  let playwrightError;
  let playwrightModule;
  try {
    await fs.mkdir(path.dirname(output), { recursive: true });
    let playwrightSucceeded = false;
    try {
      playwrightModule = await loadPlaywright();
      const chromium = playwrightModule.chromium || playwrightModule.default?.chromium || playwrightModule['module.exports']?.chromium;
      if (!chromium) throw new Error('Playwright 模块已加载，但没有 chromium 导出');
      browser = await chromium.launch({ headless: true });
      const page = await browser.newPage({ viewport, deviceScaleFactor: 1 });
      page.on('pageerror', error => pageErrors.push(String(error)));
      await page.goto(toUrl(String(args.input)), { waitUntil: 'domcontentloaded', timeout: 30000 });
      await page.waitForTimeout(8000);

      const target = page.locator(String(args.selector)).first();
      if (await target.count() === 0) throw new Error(`找不到 selector：${args.selector}`);
      await target.scrollIntoViewIfNeeded();
      await target.evaluate(element => {
        element.dataset.zyAdmissionEvidence = 'target';
        element.style.outline = '3px solid #d84b3f';
        element.style.outlineOffset = '3px';
      });
      const box = await target.boundingBox();
      if (!box) throw new Error('目标元素没有可见布局，无法截取');
      const padding = 28;
      const clip = {
        x: Math.max(0, box.x - padding),
        y: Math.max(0, box.y - padding),
        width: Math.min(viewport.width - Math.max(0, box.x - padding), box.width + padding * 2),
        height: Math.min(viewport.height - Math.max(0, box.y - padding), box.height + padding * 2)
      };
      await page.screenshot({ path: output, clip, animations: 'disabled' });
      console.log(JSON.stringify({
        ok: true,
        input: String(args.input),
        selector: String(args.selector),
        viewport,
        output,
        title: await page.title(),
        pageErrors,
        captureMode: 'selector',
        selectorApplied: true,
        browser: 'playwright'
      }, null, 2));
      process.exitCode = 0;
      playwrightSucceeded = true;
    } catch (error) {
      playwrightError = error;
      if (browser) {
        await browser.close().catch(() => {});
        browser = null;
      }
    }

    if (!playwrightSucceeded) {
      const browserPath = await findBrowser();
      if (!browserPath) throw new Error(`Playwright 截图失败，且未找到 Chrome/Edge：${playwrightError?.message || '未知错误'}`);
      const input = String(args.input);
      const fileInput = isLocalInput(input) ? path.resolve(input) : null;
      const browserLabel = /msedge/i.test(browserPath) ? 'edge-cli' : 'chrome-cli';
      const systemBrowserLabel = /msedge/i.test(browserPath) ? 'edge-system-browser' : 'chrome-system-browser';
      if (playwrightModule) {
        try {
          const chromium = playwrightModule.chromium || playwrightModule.default?.chromium || playwrightModule['module.exports']?.chromium;
          browser = await chromium.launch({
            headless: true,
            executablePath: browserPath,
            args: ['--disable-gpu', '--disable-gpu-compositing', '--hide-scrollbars']
          });
          const page = await browser.newPage({ viewport, deviceScaleFactor: 1 });
          page.on('pageerror', error => pageErrors.push(String(error)));
          await page.goto(toUrl(input), { waitUntil: 'domcontentloaded', timeout: 30000 });
          await page.waitForTimeout(8000);
          const target = page.locator(String(args.selector)).first();
          if (await target.count() === 0) throw new Error(`找不到 selector：${args.selector}`);
          await target.scrollIntoViewIfNeeded();
          await target.evaluate(element => {
            element.dataset.zyAdmissionEvidence = 'target';
            element.style.outline = '3px solid #d84b3f';
            element.style.outlineOffset = '3px';
          });
          const box = await target.boundingBox();
          if (!box) throw new Error('目标元素没有可见布局，无法截取');
          const padding = 28;
          const clip = {
            x: Math.max(0, box.x - padding),
            y: Math.max(0, box.y - padding),
            width: Math.min(viewport.width - Math.max(0, box.x - padding), box.width + padding * 2),
            height: Math.min(viewport.height - Math.max(0, box.y - padding), box.height + padding * 2)
          };
          await page.screenshot({ path: output, clip, animations: 'disabled' });
          console.log(JSON.stringify({
            ok: true,
            input,
            selector: String(args.selector),
            viewport,
            output,
            title: await page.title(),
            pageErrors,
            captureMode: 'selector-system-browser',
            selectorApplied: true,
            browser: systemBrowserLabel,
            fallbackFrom: playwrightError?.message || null
          }, null, 2));
          playwrightSucceeded = true;
        } catch (systemError) {
          playwrightError = new Error(`${playwrightError?.message || 'Playwright 默认浏览器失败'}；系统浏览器失败：${systemError.message}`);
        } finally {
          if (browser) {
            await browser.close().catch(() => {});
            browser = null;
          }
        }
      }
      if (!playwrightSucceeded) {
        try {
          const firstUrl = toUrl(input);
          const result = await captureWithBrowserCli({ browser: browserPath, url: firstUrl, output, viewport, selector: String(args.selector), input, mode: 'viewport', browserLabel });
          console.log(JSON.stringify({ ...result, fallbackFrom: playwrightError?.message || null }, null, 2));
        } catch (fileError) {
          if (!fileInput) throw fileError;
          await fs.rm(output, { force: true }).catch(() => {});
          const { root, path: relativePath } = localHttpUrl(fileInput);
          const server = await startStaticServer(root);
          try {
            const httpUrl = `http://127.0.0.1:${server.port}${relativePath}`;
            const result = await captureWithBrowserCli({ browser: browserPath, url: httpUrl, output, viewport, selector: String(args.selector), input, mode: 'viewport-http-fallback', browserLabel });
            console.log(JSON.stringify({ ...result, fallbackFrom: playwrightError?.message || null, fileCaptureError: fileError.message }, null, 2));
          } finally {
            server.child.kill();
          }
        }
      }
    }
  } catch (error) {
    console.error(JSON.stringify({ ok: false, error: String(error.message || error), pageErrors, playwrightError: playwrightError?.message || null }, null, 2));
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
  }
}
