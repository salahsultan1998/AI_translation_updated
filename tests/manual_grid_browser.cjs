// Run against an auth-disabled local instance: node tests/manual_grid_browser.cjs
// Requires Playwright and Chrome. Test requests are stubbed and never call a model.
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

async function run() {
  const browser = await chromium.launch({channel: 'chrome'});
  try {
    const context = await browser.newContext({
      permissions: ['clipboard-read', 'clipboard-write'],
      viewport: {width: 1280, height: 900}
    });
    const page = await context.newPage();
    page.setDefaultTimeout(8000);
    const errors = [];
    const submissions = [];
    let completed = false;
    const resultText = 'A long localized game message. '.repeat(20);
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/jobs**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/jobs/clear') return route.fulfill({json: {ok: true}});
      if (path === '/api/jobs') {
        submissions.push(route.request().postDataJSON());
        return route.fulfill({status: 202, json: {id: 'test-job', status: 'queued', input_mode: 'manual'}});
      }
      const submission = submissions.at(-1);
      if (path.endsWith('/manual-result')) {
        return route.fulfill({json: {rows: submission.manual_rows.map(row => ({
          ...row, translated_text: resultText
        }))}});
      }
      return route.fulfill({json: {
        id: 'test-job', language: submission.language, input_mode: 'manual',
        status: completed ? 'completed' : 'running', download_url: completed ? '/download' : null
      }});
    });
    await page.goto(process.env.LOCATOOL_TEST_URL || 'http://127.0.0.1:5000/direct');
    const rows = page.locator('#manual-rows tr');
    const cell = (row, col) => rows.nth(row).locator('.manual-cell').nth(col);
    const value = (row, col) => cell(row, col).locator('.manual-value').textContent();
    const editor = page.locator('.manual-cell-editor');
    const modifier = process.platform === 'darwin' ? 'Meta' : 'Control';
    const paste = async text => {
      await page.evaluate(data => navigator.clipboard.writeText(data), text);
      await page.keyboard.press(`${modifier}+v`);
    };

    await cell(0, 0).click();
    assert.equal(await editor.count(), 0);
    assert.equal(await cell(0, 0).getAttribute('aria-selected'), 'true');
    await paste('开始游戏\t按钮\r\n"مرحبا\nبك"\t欢迎语');
    await rows.nth(1).waitFor();
    assert.equal(await value(0, 1), '按钮');
    assert.equal(await value(1, 0), 'مرحبا\nبك');
    assert.equal(await page.locator('.manual-cell[aria-selected="true"]').count(), 4);
    await page.keyboard.press(`${modifier}+c`);
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '开始游戏\t按钮\r\n"مرحبا\nبك"\t欢迎语');
    await page.keyboard.press(`${modifier}+z`);
    await rows.nth(1).waitFor({state: 'detached'});
    assert.equal(await value(0, 0), '');

    const initialHeight = (await rows.first().boundingBox()).height;
    await cell(0, 0).dblclick();
    await editor.waitFor();
    await paste('First line\nSecond line\tstill one cell\nThird line\nFourth line');
    await page.waitForFunction(() => document.querySelector('.manual-cell-editor')?.value.includes('Fourth'));
    assert.ok((await rows.first().boundingBox()).height > initialHeight);
    await page.keyboard.press('Enter');
    assert.equal(await rows.count(), 1);
    assert.equal(await value(0, 1), '');
    await page.keyboard.press('ArrowRight');
    await page.keyboard.press('F2');
    await editor.fill('discarded');
    await page.keyboard.press('Escape');
    assert.equal(await value(0, 1), '');
    await cell(0, 0).click();
    await paste('Start\tButton\nExit\tMenu');
    await rows.nth(1).waitFor();
    await cell(0, 0).click();
    await page.keyboard.press('Shift+ArrowRight');
    await page.keyboard.press('Delete');
    assert.equal(await value(0, 0), '');
    assert.equal(await value(0, 1), '');
    await page.keyboard.press(`${modifier}+z`);
    assert.equal(await value(0, 0), 'Start');
    assert.equal(await value(0, 1), 'Button');

    const language = await page.locator('#language').inputValue();
    const model = await page.locator('#model').inputValue();
    await page.locator('#start-job').click();
    await page.waitForFunction(() => !document.querySelector('#job-status').hidden);
    assert.equal(await page.locator('#clear-manual').isDisabled(), true);
    await cell(1, 2).dblclick();
    await editor.fill('Reviewed by a translator');
    await page.keyboard.press('Enter');
    completed = true;
    await page.locator('#result-panel').waitFor({state: 'visible'});
    assert.equal(await value(0, 2), resultText);
    assert.equal(await value(1, 2), 'Reviewed by a translator');
    assert.ok((await rows.first().boundingBox()).height > initialHeight);
    assert.equal(submissions[0].manual_rows[1].source_text, 'Exit');
    if (process.env.LOCATOOL_SCREENSHOT) {
      await page.locator('#manual-input').screenshot({path: process.env.LOCATOOL_SCREENSHOT});
    }
    page.once('dialog', dialog => dialog.dismiss());
    await page.locator('#clear-manual').click();
    assert.equal(await rows.count(), 2);
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#clear-manual').click();
    assert.equal(await rows.count(), 1);
    assert.equal(await value(0, 0), '');
    assert.equal(await page.locator('#job-status').isHidden(), true);
    assert.equal(await page.locator('#result-panel').isHidden(), true);
    assert.equal(await page.locator('#language').inputValue(), language);
    assert.equal(await page.locator('#model').inputValue(), model);
    await paste('New batch');
    await cell(0, 0).locator('.manual-value').getByText('New batch').waitFor();
    await page.locator('#start-job').click();
    await cell(0, 2).getByText(resultText.trim(), {exact: true}).waitFor();
    assert.equal(submissions[1].manual_rows[0].source_text, 'New batch');

    const largePasteMs = await cell(0, 0).evaluate(target => {
      target.focus();
      const clipboardData = new DataTransfer();
      clipboardData.setData('text/plain', Array.from({length: 10000}, (_, i) => `Text ${i}\tContext`).join('\n'));
      const start = performance.now();
      target.dispatchEvent(new ClipboardEvent('paste', {clipboardData, bubbles: true, cancelable: true}));
      return performance.now() - start;
    });
    assert.equal(await rows.count(), 10000);
    assert.equal(await value(9999, 0), 'Text 9999');
    assert.equal(await editor.count(), 0);
    assert.deepEqual(errors, []);
    console.log(`Manual worksheet checks passed. 10,000-row paste handler: ${Math.round(largePasteMs)} ms.`);
  } finally {
    await browser.close();
  }
}

run().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
