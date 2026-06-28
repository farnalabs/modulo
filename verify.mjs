import { chromium } from 'playwright';

const BASE = 'https://demo.modulo.run';
const SCREENSHOT_DIR = 'C:\\Users\\dunca\\AppData\\Local\\Temp';

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();

  const results = [];

  async function screenshot(name) {
    await page.screenshot({ path: `${SCREENSHOT_DIR}\\${name}.png`, fullPage: true });
  }

  // 1-3. Navigate and screenshot login
  console.log('=== Step 1: Navigate to https://demo.modulo.run/ ===');
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await screenshot('verify-01-login');

  // 4. Page title and URL
  const title = await page.title();
  const url = page.url();
  console.log(`Title: "${title}"`);
  console.log(`URL: ${url}`);
  results.push({ page: 'Login', title, url });

  // 5. Check login form
  const bodyText = await page.innerText('body');
  const hasSignIn = bodyText.includes('Sign in') || bodyText.includes('sign in') || bodyText.includes('Sign In');
  const hasDemo = bodyText.includes('demo');
  console.log(`Has "Sign in" text: ${hasSignIn}`);
  console.log(`Has "demo" text: ${hasDemo}`);

  // Check for email/password inputs
  const emailInput = page.locator('input[type="email"], input[name="email"], input[id*="email"], input[placeholder*="email"], input[placeholder*="Email"]').first();
  const passwordInput = page.locator('input[type="password"], input[name="password"], input[id*="password"]').first();
  const hasEmailInput = await emailInput.isVisible().catch(() => false);
  const hasPasswordInput = await passwordInput.isVisible().catch(() => false);
  console.log(`Email input visible: ${hasEmailInput}`);
  console.log(`Password input visible: ${hasPasswordInput}`);

  // Find sign-in button
  const signInButton = page.locator('button, input[type="submit"], a').filter({ hasText: /sign\s*in/i }).first();
  const hasSignInButton = await signInButton.isVisible().catch(() => false);
  console.log(`Sign-in button visible: ${hasSignInButton}`);

  // 6. Fill credentials and sign in
  console.log('\n=== Step 2: Sign in with demo/demo ===');
  if (hasEmailInput) {
    await emailInput.fill('');
    await emailInput.type('demo', { delay: 30 });
  }
  if (hasPasswordInput) {
    await passwordInput.fill('');
    await passwordInput.type('demo', { delay: 30 });
  }
  if (hasSignInButton) {
    await signInButton.click();
  }

  // 7. Wait for redirect to dashboard
  try {
    await page.waitForURL('**/dashboard**', { timeout: 15000 });
    console.log('Redirected to /dashboard');
  } catch {
    console.log('Did not redirect to /dashboard — trying to wait for any navigation...');
    try {
      await page.waitForURL(u => !u.includes('/auth') && !u.includes('/login') && !u.includes('/signin'), { timeout: 10000 });
      console.log(`Current URL after wait: ${page.url()}`);
    } catch {
      console.log('No navigation detected — current URL:', page.url());
    }
  }

  await page.waitForTimeout(2000);
  await screenshot('verify-02-dashboard');

  // 8. Check sidebar
  console.log('\n=== Step 3: Check sidebar ===');
  const sidebarTexts = ['Library', 'Settings', 'Dashboard', 'Free', 'Navigation'];
  const sidebarVisible = {};
  for (const text of sidebarTexts) {
    sidebarVisible[text] = await page.locator(`text=${text}`).first().isVisible().catch(() => false);
  }
  console.log('Sidebar/dashboard element visibility:', JSON.stringify(sidebarVisible, null, 2));

  // 9. Navigate to /library
  console.log('\n=== Step 4: Navigate to /library ===');
  try {
    await page.goto(`${BASE}/library`, { waitUntil: 'networkidle', timeout: 20000 });
  } catch {
    console.log('Navigation timeout — continuing with current state');
  }
  await page.waitForTimeout(2000);
  await screenshot('verify-03-library');
  const libraryTitle = await page.title();
  const libraryBody = await page.innerText('body').catch(() => '(could not read)');
  console.log(`Library page title: "${libraryTitle}"`);
  console.log(`Library URL: ${page.url()}`);
  results.push({ page: 'Library', title: libraryTitle, url: page.url() });

  // 10. Navigate to /settings/runtime-config
  console.log('\n=== Step 5: Navigate to /settings/runtime-config ===');
  try {
    await page.goto(`${BASE}/settings/runtime-config`, { waitUntil: 'networkidle', timeout: 20000 });
  } catch {
    console.log('Navigation timeout — continuing with current state');
  }
  await page.waitForTimeout(2000);
  await screenshot('verify-04-runtime-config');
  const rtTitle = await page.title();
  const rtBody = await page.innerText('body').catch(() => '(could not read)');
  console.log(`Runtime config page title: "${rtTitle}"`);
  console.log(`Runtime config URL: ${page.url()}`);
  results.push({ page: 'Runtime Config', title: rtTitle, url: page.url(), bodyPreview: rtBody.substring(0, 500) });

  // Summary
  console.log('\n=== VERIFICATION SUMMARY ===');
  for (const r of results) {
    console.log(`\n--- ${r.page} ---`);
    console.log(`  Title:  ${r.title}`);
    console.log(`  URL:    ${r.url}`);
    console.log(`  Status: ${r.url.includes('error') || r.title.includes('Not Found') || r.title.includes('Error') ? '⚠️ ISSUE' : '✅ OK'}`);
  }
  console.log(`\nScreenshots saved to ${SCREENSHOT_DIR}\\verify-*.png`);

  await browser.close();
}

main().catch(err => {
  console.error('Script failed:', err);
  process.exit(1);
});
