# Local Code Review

Review local Git or SVN changes in your browser, leave comments on the diff,
and send questions or change requests to **Codex, Claude Code, Pi, or another
coding agent**. Import the agent's replies into the original comment threads
and review the updated code without losing the earlier discussion.

The app works with a new or existing agent session that can access your local
files. You copy a generated prompt into your agent; the agent answers questions
or makes changes and writes a response file for you to import. Local Code Review
needs no API keys and makes no model calls itself. You use your agent separately,
with its usual access requirements and costs.

## Install with your coding agent

Paste this into Claude Code, Codex, or another agent that can run local commands:

```text
Install and run Local Code Review from
https://github.com/lukemoore/local-code-review.
Clone it into a local-code-review folder, or use an existing checkout if
one is already present. Read its README, check that Python 3.10+ and Git
are available (and SVN if I want to review SVN changes), then run
python3 start.py from the checkout (py start.py on Windows).
Keep the server running. The launcher opens my browser automatically.
If that fails, give me the local URL to open manually.
```

The launcher sets up the app and opens your browser. To install it yourself,
follow the manual steps below.

## Manual setup

Requires **Python 3.10 or later** and **Git** to clone this project. Install
**SVN** too if you want to review SVN working copies.

```sh
git clone https://github.com/lukemoore/local-code-review.git
cd local-code-review
python3 start.py
```

On Windows, use `py start.py` instead.

The launcher creates a virtual environment, installs dependencies, sets up the
local database, and opens <http://127.0.0.1:8765> in your browser. The first run
needs internet access to install dependencies. Later starts install them only
when `requirements.txt` changes.

Keep the terminal open while using the app. Press **Ctrl+C** to stop it.
Next time, run `python3 start.py` again. You can also run the script by its full
path from any directory. If the browser does not open, use the URL shown in the
terminal.

## Review changes with your agent

1. Select **New review**, then **Browse…** to choose a local Git or SVN
   working-copy folder. You can also enter its path. Choose a subdirectory to
   limit the review. The app saves the current diff as a *snapshot*, so you can
   return to it after the code changes.
2. Select code text on the new side of the diff and click **Add comment**.
   You can also select a line number, then Shift-click another line number to
   select a range in the same hunk. Write your question or change request and
   select **Save comment**. Use **Add session-wide comment** for broader
   questions, deleted files, binary files, or SVN properties.
3. Select **Send comments to agent** and paste the generated prompt into your
   coding agent, preferably in a session opened in the reviewed working copy.
   The prompt includes unanswered requests from unresolved threads across all
   snapshots, their conversations, and the saved diffs needed to understand them.
4. Let the agent answer your questions or implement your requests. The prompt
   tells it where and how to write its response file. If it cannot write that
   file, follow the prompt's instructions to save its JSON response manually.
5. Select **Import replies**. Replies appear under their original comments.
   The app also captures a new snapshot if the working-copy diff changed.
   Use **View replies** to return to the discussion. If the code refresh fails,
   your imported replies are still saved; use **Reload diffs** to retry.
6. Add a reply to ask a follow-up, then send comments to the agent again.
   Use **Resolve** when a thread is complete. Importing an answer does not
   resolve the thread for you.

Use **Reload diffs** on the latest snapshot to capture changes made outside
this flow. Earlier snapshots and their comments stay available in the snapshot
menu. The review menu lets you switch between projects without losing reviews.

### Comments and replies

- Use **Edit** to change a comment before it has replies. Each review also has
  one session-wide thread, shared across snapshots; use **Edit session-wide
  comment** to change its latest request.
- Editing a comment does not change a prompt you already copied. Send comments
  again after saving an edit. An answer to an earlier request stays visible
  without clearing your newer request.
- Unsaved comments, edits, and replies survive snapshot navigation, but not a
  browser reload. Save them before reloading or closing the page.
- You can delete a comment that has never been sent to an agent. After sending
  it, use **Resolve** or **Reopen** to keep its history.

The red toolbar badge counts replies that the agent flagged for your attention.
Use **Jump to first reply needing review** to find them, then **Mark reviewed**
after reading each one. This clears the reply from the queue without resolving
the thread or requesting another answer. Routine fixes may not enter this queue;
their replies still appear in the thread.

Use **Previous comment** and **Next comment**, or click the bands on the right,
to move between threads. Blue bands mark open threads; green bands mark resolved
threads. **Latest snapshot** returns to the current code. The app remembers
your reading position for each snapshot in your browser.

## What you can review

- **Git:** staged and unstaged tracked changes together, compared with HEAD
  (or an empty tree before the first commit). Renames appear as a deletion and
  an addition.
- **SVN:** local working-copy changes captured with `svn diff --internal-diff`.
- **New files:** add them with Git or SVN first. Untracked files do not appear.
- **Submodules and externals:** open a separate review for file changes inside
  a Git submodule or SVN external.
- **Binary files and properties:** notices appear as text. Line comments apply
  to text on the new side; use a session-wide comment for other changes.

A snapshot saves the diff; it does not lock your files. Avoid editing during
capture. The agent is instructed to check current files before changing code
from an older snapshot. The app does not commit, update, add, or revert files.

Diff capture has a 60-second timeout and a 20 MB output limit. Non-UTF-8 text
appears with replacement characters; the app does not write that text back to
source files.

## Local data and privacy

This is a single-user tool. The server listens only on your computer, with no
login or remote access support. SVN uses your existing local configuration and
credentials.

Reviews, snapshots, and comments are stored in `var/reviews.sqlite3`. Agent
prompts, saved diffs, and response files are stored in `var/handoffs/`. These
files contain source code and review comments. Keep them private, and consider
what code you share with your chosen agent.

The app uses Django, SQLite, and plain JavaScript. It needs no frontend build
step, worker, or external database.

## Update and back up

Stop the server and back up the whole `var/` directory outside this checkout.
Then run:

```sh
git pull --ff-only
python3 start.py
```

The launcher applies database updates and keeps existing reviews. Git ignores
`var/`, so normal pulls do not replace your data. Deleting the checkout or
running `git clean -x` can delete it. To restore a backup, stop the server and
copy your saved `var/` directory back into the checkout.

## Development checks

After the launcher has set up the environment:

```sh
.venv/bin/python manage.py check
.venv/bin/python manage.py test review
```

On Windows, use `.venv\Scripts\python.exe` for these commands. The tests require
`git`, `svn`, and `svnadmin`. They use isolated local repositories, temporary
files, and a test database, without calling an agent or paid API.

Browser tests also require Node.js, Playwright, and Chrome. With Playwright
installed in your Node environment, run:

```sh
node --test review/drafts.test.cjs
```

These tests use local fixture data and do not access your review database.
