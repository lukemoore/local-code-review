// Run with Node and Playwright installed. All requests use isolated fixtures.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {chromium} = require('playwright');

for (const width of [1280, 480]) {
  test(`text selections and draft navigation at ${width}px`, async () => {
    const browser = await chromium.launch({headless: true, channel: 'chrome'});
    try {
      const page = await browser.newPage({viewport: {width, height: 900}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const user = (id, body) => ({id, author: 'user', body});
      const comment = (id, messages, can_delete = false) => ({
        id, messages, can_delete, path: '', resolved: false,
      });
      const snapshots = [1, 2].map(id => ({
        id, review_id: 1, title: 'Draft test', working_copy: '/fixture',
        created: '2026-11-13', revision: '1', batches: [], review_queue: [],
        has_comments_to_send: true,
        snapshots: [2, 1].map(id => ({id, number: id, created: '2026-11-13'})),
        files: [{path: 'sample.txt', rows: [1, 2, 3].map(line => ({
          kind: 'add', old: null, new: line, text: `line ${line}`,
        }))}, {path: 'translation/requirements.txt', rows: [
          {kind: 'hunk', old: null, new: null, text: '@@ -0,0 +1,4 @@'},
          ...['openai>=2.54.0,<3', 'regex>=2023.10.3', 'wcwidth>=0.2.8',
            'tiktoken>=0.13.0'].map((text, index) => ({
            kind: 'add', old: null, new: index + 1, text,
          })),
          {kind: 'meta', old: null, new: null, text: ''},
          {kind: 'remove', old: 10, new: null, text: 'removed dependency'},
          {kind: 'hunk', old: null, new: null, text: '@@ -20 +20 @@'},
          {kind: 'context', old: 20, new: 20, text: 'later dependency'},
        ]}],
        comments: id === 1 ? [
          comment('reply', [user(1, 'Question'), {id: 2, author: 'codex', body: 'Answer', earlier_request: true, answered_version: 1}]),
          comment('edit', [user(3, 'Original')], true),
          comment('other', [user(4, 'Other')], true),
          comment('resolve', [user(5, 'Resolve me')]),
        ] : [],
      }));
      let failSave = false;
      let saveGate = null;
      let notifySave = null;
      const mutations = [];
      await page.route('http://review.test/**', async route => {
        const request = route.request();
        const url = new URL(request.url());
        if (url.pathname.startsWith('/api/')) {
          const action = url.pathname.split('/')[2];
          if (action === 'reviews') return route.fulfill({json: {
            reviews: [{id: 1, title: 'Draft test', latest_snapshot: 1}],
          }});
          if (action === 'snapshot') return route.fulfill({json: snapshots[Number(url.searchParams.get('id')) - 1]});
          if (failSave) return route.fulfill({status: 400, json: {error: 'Save failed'}});
          if (notifySave) notifySave();
          if (saveGate) await saveGate;
          const data = request.postDataJSON();
          mutations.push({action, data});
          const thread = snapshots[0].comments.find(c => c.id === data.comment_id);
          if (action === 'delete') snapshots[0].comments = snapshots[0].comments.filter(c => c !== thread);
          if (action === 'resolve') thread.resolved = data.resolved;
          if (action === 'edit') thread.messages.filter(m => m.author === 'user').at(-1).body = data.body;
          if (action === 'reply') thread.messages.push(user(10, data.body));
          return route.fulfill({json: {ok: true}});
        }
        if (url.pathname.startsWith('/static/')) {
          const name = url.pathname.slice('/static/'.length);
          return route.fulfill({body: readFileSync(join(__dirname, 'static', name)),
            contentType: name.endsWith('.js') ? 'application/javascript' : name.endsWith('.svg') ? 'image/svg+xml' : 'text/css'});
        }
        const html = readFileSync(join(__dirname, 'templates/review/index.html'), 'utf8')
          .replace('{% load static %}', '').replace(/{% static '([^']+)' %}/g, '/static/$1');
        return route.fulfill({body: html, contentType: 'text/html'});
      });
      await page.goto('http://review.test/?snapshot=1');
      const idle = () => page.waitForFunction(() => snapshot && !busy);
      const click = async locator => { await locator.click(); await idle(); };
      const thread = id => page.locator(`#comment-${id}`);
      const reply = () => thread('reply').locator('textarea');
      const edit = () => thread('edit').locator('textarea');
      const composer = page.locator('#comment-body');
      await idle();
      const requirements = page.locator('tr[data-path="translation/requirements.txt"]').first();
      await requirements.evaluate(row => row.scrollIntoView({block: 'center'}));
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const selectRequirements = async mode => {
        await page.evaluate(mode => {
          const rows = [...document.querySelectorAll('tr[data-path="translation/requirements.txt"]')];
          const source = row => row.querySelector('.source').lastChild;
          const range = document.createRange();
          range.setStart(source(rows[0]), 0);
          if (mode === 'blank') range.setEnd(rows[3].nextElementSibling.querySelector('.source'), 0);
          if (mode === 'number') range.setStart(rows[0].querySelector('.line-number'), 0);
          if (mode === 'number' || mode === 'reverse') range.setEnd(source(rows[3]), source(rows[3]).length);
          if (mode === 'next-start') range.setEnd(source(rows[1]), 0);
          if (mode === 'gap') range.setEnd(source(rows[4]), source(rows[4]).length);
          if (mode === 'deleted') range.selectNodeContents(rows[3].nextElementSibling.nextElementSibling.querySelector('.source'));
          if (mode === 'header') range.selectNodeContents(rows[0].previousElementSibling.querySelector('.source'));
          if (mode === 'cross-file') {
            range.setStart(document.querySelector('tr[data-path="sample.txt"] .source').lastChild, 0);
            range.setEnd(source(rows[0]), source(rows[0]).length);
          }
          const selection = getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          if (mode === 'reverse') selection.setBaseAndExtent(
            range.endContainer, range.endOffset, range.startContainer, range.startOffset);
        }, mode);
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      };
      for (const mode of ['blank', 'number', 'reverse', 'next-start']) {
        await selectRequirements(mode);
        assert.equal(await page.locator('#selection-comment').isVisible(), true, mode);
        await click(page.locator('#selection-comment'));
        assert.deepEqual(await page.evaluate(() => selection), {
          path: 'translation/requirements.txt', anchor: 1, start: 1,
          end: mode === 'next-start' ? 1 : 4,
        });
        await click(page.locator('#cancel-comment'));
      }
      for (const mode of ['gap', 'deleted', 'header', 'cross-file']) {
        await selectRequirements(mode);
        assert.equal(await page.locator('#selection-comment').isVisible(), false, mode);
      }
      await page.evaluate(() => getSelection().removeAllRanges());
      assert.match(await thread('reply').innerText(), /Answer to an earlier request/);
      await reply().fill('Unsent reply');
      await click(thread('edit').getByRole('button', {name: 'Edit', exact: true}));
      await edit().fill('Unsent edit');
      // Select upwards to verify that the anchor and both endpoints survive.
      await page.evaluate(() => { selectLine('sample.txt', 3, false); selectLine('sample.txt', 1, true); });
      await composer.fill('Unsent comment');
      const checkDrafts = async () => {
        assert.equal(await reply().inputValue(), 'Unsent reply');
        assert.equal(await edit().inputValue(), 'Unsent edit');
        assert.equal(await edit().isVisible(), true);
        assert.equal(await composer.inputValue(), 'Unsent comment');
        assert.equal(await page.locator('#composer').isVisible(), true);
        assert.deepEqual(await page.evaluate(() => selection), {path: 'sample.txt', anchor: 3, start: 1, end: 3});
        assert.equal(await page.locator('tr.selected').count(), 3);
      };
      await click(thread('other').getByRole('button', {name: 'Edit', exact: true}));
      await thread('other').locator('textarea').fill('Saved other edit');
      await click(thread('other').getByRole('button', {name: 'Save changes'}));
      await checkDrafts();
      await click(thread('resolve').getByRole('button', {name: 'Resolve', exact: true}));
      await checkDrafts();
      await click(thread('other').getByRole('button', {name: 'Delete'}));
      await checkDrafts();
      await page.selectOption('#snapshots', '2'); await idle();
      assert.equal(await page.locator('#composer').isVisible(), false);
      await click(page.locator('#general'));
      await composer.fill('Second snapshot draft');
      await page.selectOption('#snapshots', '1'); await idle();
      await checkDrafts();
      failSave = true;
      for (const button of [thread('reply').getByRole('button', {name: 'Add reply'}),
        thread('edit').getByRole('button', {name: 'Save changes'}),
        page.locator('#composer button[type=submit]')]) {
        await click(button);
        assert.equal(await page.locator('#notice').innerText(), 'Save failed');
        await checkDrafts();
      }
      failSave = false;
      // Text typed during a slow save belongs to a new draft.
      let finishSave;
      const saveStarted = new Promise(resolve => { notifySave = resolve; });
      saveGate = new Promise(resolve => { finishSave = resolve; });
      await thread('reply').getByRole('button', {name: 'Add reply'}).click();
      await saveStarted;
      notifySave = null;
      await reply().fill('Typed during save');
      finishSave();
      await idle();
      saveGate = null;
      await page.selectOption('#snapshots', '1'); await idle();
      assert.equal(await reply().inputValue(), 'Typed during save');
      await reply().fill('Unsent reply');
      await click(thread('edit').getByRole('button', {name: 'Cancel', exact: true}));
      assert.equal(await edit().isVisible(), false);
      await click(thread('edit').getByRole('button', {name: 'Edit', exact: true}));
      assert.equal(await edit().inputValue(), 'Original');
      await edit().fill('Saved edit');
      await click(thread('edit').getByRole('button', {name: 'Save changes'}));
      assert.equal(await reply().inputValue(), 'Unsent reply');
      assert.equal(await composer.inputValue(), 'Unsent comment');
      await click(thread('reply').getByRole('button', {name: 'Add reply'}));
      await page.selectOption('#snapshots', '1'); await idle();
      assert.equal(await reply().inputValue(), '');
      await click(page.locator('#composer button[type=submit]'));
      assert.equal(await page.locator('#composer').isVisible(), false);
      assert.deepEqual(mutations.find(m => m.action === 'comment').data, {
        snapshot_id: 1, path: 'sample.txt', anchor: 3, start: 1, end: 3, body: 'Unsent comment',
      });
      await click(thread('edit').getByRole('button', {name: 'Edit', exact: true}));
      await edit().fill('Deleted draft');
      await click(thread('edit').getByRole('button', {name: 'Delete'}));
      assert.equal(await page.evaluate(() => threadDrafts.has('edit')), false);
      await page.selectOption('#snapshots', '2'); await idle();
      assert.equal(await composer.inputValue(), 'Second snapshot draft');
      await click(page.locator('#cancel-comment'));
      await page.selectOption('#snapshots', '1'); await idle();
      await page.selectOption('#snapshots', '2'); await idle();
      assert.equal(await page.locator('#composer').isVisible(), false);
      const sessionComment = {...snapshots[0].comments.find(c => c.id === 'reply'), snapshot_id: 1};
      snapshots.forEach(item => { item.session_comment = sessionComment; });
      await page.selectOption('#snapshots', '1'); await idle();
      await page.selectOption('#snapshots', '2'); await idle();
      assert.equal(await page.locator('#general').innerText(), 'Edit session-wide comment');
      await click(page.locator('#general'));
      assert.equal(await page.locator('#snapshots').inputValue(), '1');
      assert.equal(await composer.inputValue(), 'Unsent reply');
      await composer.fill('Edited session request');
      await click(page.locator('#composer button[type=submit]'));
      assert.equal(mutations.at(-1).action, 'edit');
      assert.equal(mutations.at(-1).data.comment_id, 'reply');
      assert.equal(mutations.at(-1).data.body, 'Edited session request');
      assert.equal(await page.locator('#general').innerText(), 'Edit session-wide comment');
      assert.deepEqual(errors, []);
    } finally {
      await browser.close();
    }
  });
}
