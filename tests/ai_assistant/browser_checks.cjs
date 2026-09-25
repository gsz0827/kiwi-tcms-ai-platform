// Browser regression checks for the real scripts in a minimal CSP-enabled page.
const { chromium } = require('playwright');
const http = require('node:http');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const path = require('node:path');
const root = path.resolve(__dirname, '../../tcms/ai_assistant/static/ai_assistant');
const form = `<form id="generation-form" method="post" action="/submit">
<input name="submission_token" value="stable-token">
<button type="submit" id="analyze-button" name="action" value="analyze">分析需求</button>
<button type="submit" id="generate-button" name="action" value="generate">生成测试用例</button>
</form><div id="generation-progress" style="display:none"></div>
<script src="/requirement_form.js"></script>`;
const job = `<div id="job-panel" data-status-url="/status"></div>
<div id="job-progress-bar" class="active"></div><div id="job-status-label"></div>
<div id="job-progress-text"></div><div id="job-stage"></div><div id="job-actions"></div>
<div id="job-result" style="display:none">任务已完成</div><a id="result-link" style="display:none">查看结果</a>
<div id="connection-result"></div><div id="job-error"></div><script src="/job_detail.js"></script>`;
const resource = `<section data-resource-browser="requirement">
<input class="kiwi-resource-filter"><p class="kiwi-resource-no-match" hidden></p>
</section><div class="kiwi-resource-modal" id="kiwi-folder-manager-requirement"></div>
<script src="/platform_resource_browser.js"></script>`;
const server = http.createServer(async (req, res) => {
  res.setHeader('Content-Security-Policy', "script-src 'self'");
  if (req.url.endsWith('.js')) {
    res.setHeader('Content-Type', 'application/javascript');
    return res.end(fs.readFileSync(path.join(root, req.url.slice(1))));
  }
  if (req.url === '/submit') {
    let body = '';
    for await (const chunk of req) body += chunk;
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify(Object.fromEntries(new URLSearchParams(body))));
  }
  if (req.url === '/status') {
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify({status:'completed',status_label:'已完成',progress:100,
      stage:'<img src=x onerror=alert(1)>',is_terminal:true,result_url:'/result',result:{reply:'OK',elapsed_ms:12}}));
  }
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end(req.url === '/job' ? job : req.url === '/resource' ? resource : form);
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless:true,
    executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const origin = `http://127.0.0.1:${server.address().port}`;
    for (const action of ['analyze', 'generate']) {
      await page.goto(origin);
      await Promise.all([page.waitForURL('**/submit'), page.click(`#${action}-button`)]);
      const body = JSON.parse(await page.locator('body').innerText());
      assert.equal(body.action, action);
      assert.equal(body.submission_token, 'stable-token');
      console.log(`PASS browser form submits ${action} with disabled buttons and CSP`);
    }
    await page.goto(origin+'/job');
    await page.locator('#job-result').waitFor({state:'visible'});
    assert.equal(await page.locator('#job-status-label').innerText(), '已完成');
    assert.equal(await page.locator('#job-progress-text').innerText(), '100%');
    assert.equal(await page.locator('#job-stage img').count(), 0);
    assert.equal(await page.locator('#result-link').getAttribute('href'), '/result');
    assert.deepEqual(errors, []);
    console.log('PASS browser polls status under CSP and renders stage as text');
    await page.goto(origin+'/resource');
    assert.equal(
      await page.locator('#kiwi-folder-manager-requirement').evaluate(
        node => node.parentElement === document.body
      ),
      true
    );
    assert.deepEqual(errors, []);
    console.log('PASS resource browser moves modals under CSP');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); server.close(); process.exitCode=1; });
