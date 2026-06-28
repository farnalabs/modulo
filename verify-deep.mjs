import { chromium } from 'playwright';

const BASE = 'https://demo.modulo.run';

async function examinePage(page, label) {
  console.log(`\n========== ${label} (${page.url()}) ==========`);
  console.log(`Title: "${await page.title()}"`);

  // Get all text content
  const text = await page.innerText('body').catch(() => '(failed)');
  const lines = text.split('\n').map(l => l.trim()).filter(l => l);
  console.log('Body text lines:');
  lines.forEach((l, i) => {
    if (l.length > 100) l = l.substring(0, 100) + '...';
    console.log(`  [${i}] ${l}`);
  });

  // List all interactive elements
  const buttons = await page.locator('button, [role="button"], a[href]').all().catch(() => []);
  console.log(`\nButtons/links (${buttons.length}):`);
  for (const btn of buttons) {
    const tag = await btn.evaluate(el => el.tagName);
    const text2 = (await btn.innerText().catch(() => '')).trim() || (await btn.getAttribute('aria-label').catch(() => '')) || '';
    const href = await btn.getAttribute('href').catch(() => '');
    const visible = await btn.isVisible().catch(() => false);
    if (text2 || href) console.log(`  <${tag}> "${text2}" href="${href}" visible=${visible}`);
  }

  // Input fields
  const inputs = await page.locator('input').all().catch(() => []);
  console.log(`\nInputs (${inputs.length}):`);
  for (const inp of inputs) {
    const type = await inp.getAttribute('type').catch(() => '?');
    const name = await inp.getAttribute('name').catch(() => '?');
    const placeholder = await inp.getAttribute('placeholder').catch(() => '?');
    const visible = await inp.isVisible().catch(() => false);
    console.log(`  type=${type} name=${name} placeholder="${placeholder}" visible=${visible}`);
  }

  // HTML structure preview
  const html = await page.content().catch(() => '');
  // Get key structural elements
  const hasNav = html.includes('<nav') || html.includes('nav>');
  const hasSidebar = html.includes('sidebar') || html.includes('Sidebar') || html.includes('side-menu') || html.includes('SideNav');
  const hasHeader = html.includes('<header') || html.includes('header>');
  const hasMain = html.includes('<main') || html.includes('main>');
  const hasAside = html.includes('<aside') || html.includes('aside>');
  console.log(`\nStructure: nav=${hasNav} sidebar=${hasSidebar} header=${hasHeader} main=${hasMain} aside=${hasAside}`);

  return { text, lines };
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();

  // 1. Login page
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await examinePage(page, 'LOGIN PAGE');

  // Try to find and interact with login form — look for any input fields
  const allInputs = await page.locator('input').all();
  if (allInputs.length >= 2) {
    console.log('\n--- Attempting login ---');
    await allInputs[0].fill('demo');
    await allInputs[1].fill('demo');
    // Find submit button
    const submitBtn = await page.locator('button[type="submit"], button:has-text("Sign"), button:has-text("Log"), button:has-text("Continue"), [type="submit"]').first();
    const submitVisible = await submitBtn.isVisible().catch(() => false);
    console.log(`Submit button visible: ${submitVisible}`);
    if (submitVisible) {
      await submitBtn.click();
      await page.waitForTimeout(3000);
      console.log(`After login URL: ${page.url()}`);
    }
  }

  // 2. Dashboard (post-login or direct)
  console.log('\n--- Checking main page content ---');
  await examinePage(page, 'POST-LOGIN / MAIN PAGE');

  // 3. Library
  await page.goto(`${BASE}/library`, { waitUntil: 'networkidle', timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(2000);
  await examinePage(page, 'LIBRARY');

  // 4. Runtime config
  await page.goto(`${BASE}/settings/runtime-config`, { waitUntil: 'networkidle', timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(2000);
  await examinePage(page, 'RUNTIME CONFIG');

  await browser.close();
}

main().catch(err => { console.error('FATAL:', err); process.exit(1); });
