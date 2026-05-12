const fs = require('fs');
const readline = require('readline');
const { pathToFileURL } = require('url');

let puppeteer;
let browser;
let config = {};

const puppeteerModule = process.argv[2];
const configPath = process.argv[3];
const chromePath = process.argv[4] || undefined;

function send(message) {
  process.stdout.write(JSON.stringify(message) + '\n');
}

function loadPuppeteer() {
  if (puppeteer) {
    return puppeteer;
  }
  let loaded = require(puppeteerModule);
  if (typeof loaded.launch !== 'function' && loaded.default) {
    loaded = loaded.default;
  }
  puppeteer = loaded;
  return puppeteer;
}

function loadConfig() {
  try {
    config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
  } catch {
    config = {};
  }
}

async function getBrowser() {
  if (browser && browser.isConnected()) {
    return browser;
  }

  const launchOpts = {
    headless: 'new',
    args: [
      ...(config.args || []),
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-gpu',
      '--disable-dev-shm-usage',
    ],
  };
  if (chromePath) {
    launchOpts.executablePath = chromePath;
  }

  browser = await loadPuppeteer().launch(launchOpts);
  browser.on('disconnected', () => {
    browser = null;
  });
  return browser;
}

async function closeBrowser() {
  const current = browser;
  browser = null;
  if (current) {
    try {
      await current.close();
    } catch (err) {
      console.error(`browser close failed: ${err && err.stack ? err.stack : err}`);
    }
  }
}

async function render(req) {
  const activeBrowser = await getBrowser();
  const page = await activeBrowser.newPage();
  try {
    const viewport = req.viewport || { width: 800, height: 600 };
    await page.setViewport(viewport);

    const timeoutMs = req.navigationTimeoutMs || 5000;
    await page.goto(pathToFileURL(req.htmlPath).href, {
      waitUntil: req.waitUntil || 'domcontentloaded',
      timeout: timeoutMs,
    });

    const selector = req.selector || 'body';
    const el = await page.$(selector);
    if (!el) {
      throw new Error(`selector not found: ${selector}`);
    }
    const box = await el.boundingBox();
    if (!box) {
      throw new Error(`selector has no bounding box: ${selector}`);
    }

    const pad = Number.isFinite(req.pad) ? req.pad : 0;
    await page.screenshot({
      path: req.outputPath,
      clip: {
        x: Math.max(0, box.x - pad),
        y: Math.max(0, box.y - pad),
        width: box.width + pad * 2,
        height: box.height + pad * 2,
      },
      type: 'png',
    });
  } finally {
    await page.close();
  }
}

async function handleLine(line) {
  let req;
  try {
    req = JSON.parse(line);
  } catch (err) {
    send({ id: null, ok: false, error: `invalid json: ${err.message}` });
    return;
  }

  if (req.method === 'shutdown') {
    await closeBrowser();
    send({ id: req.id, ok: true });
    process.exit(0);
  }

  try {
    await render(req);
    send({ id: req.id, ok: true });
  } catch (err) {
    console.error(err && err.stack ? err.stack : err);
    send({ id: req.id, ok: false, error: err && err.message ? err.message : String(err) });
  }
}

async function main() {
  loadConfig();
  const rl = readline.createInterface({
    input: process.stdin,
    crlfDelay: Infinity,
  });

  for await (const line of rl) {
    if (!line.trim()) {
      continue;
    }
    await handleLine(line);
  }

  await closeBrowser();
}

process.on('SIGTERM', () => {
  closeBrowser().finally(() => process.exit(0));
});
process.on('SIGINT', () => {
  closeBrowser().finally(() => process.exit(0));
});

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
