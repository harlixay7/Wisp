# Writing a good review request

A Wisp review is only as useful as the request behind it. "Review my code"
gets you a polite skim. A request that says what was decided, what must not
change and which claims to test gets you findings you can act on. This guide
shows how to write the second kind. It applies whether your coding assistant
sends the request through the MCP tool or you run the command line yourself.

Examples here use `<wisp-repo>` for the folder Wisp lives in. Real paths are
filled in when the request is sent.

## 1. What to send to the reviewer, and what to keep

The reviewer is a second model with no stake in your plan. That independence
is what you're paying for, so spend it on judgment rather than chores your
assistant can do itself.

| Send it to the reviewer | Keep it with your assistant |
| --- | --- |
| A second opinion on decisions your assistant made alone | Running tests, linters and compile checks |
| Testing claims the plan depends on | Reading files your assistant already has open |
| "What should we ask the user before building this?" | Mechanical scans (searches, AST or contract checks) |
| Alternative designs worth considering | Applying patches |
| Checking that finished work is really wired up | Anything that needs the assistant's conversation history |

A request that only says "run the tests" pays for a consultant and gets a
shell script.

## 2. The parts of a request

Every field except `prompt` is optional, but each one makes the answer
sharper. The order below is the order that works best when you write them.

1. **`skills`**: name the playbook to follow, such as
   `adversarial-plan-hardening-engine` or `pre-merge-diff-audit`. Add up to
   three more in `recommended_skills` when the task touches their area. The
   [README](../README.md#review-playbooks) lists all seventeen. Don't tell
   the reviewer to ignore the playbooks; leave `skills` out instead.
2. **`workspace`**: the project folder the file paths refer to. It defaults
   to the folder the server or command runs in. On the command line, use
   `--workspace` instead.
3. **`context`**: facts about the environment, not instructions. The
   language and versions, how you verify changes (`pytest -q`,
   `ruff check`), known traps, and what was already tried and failed.
4. **`prompt`**: the request itself. Section 3 describes what to put in it.
5. **`claims_to_falsify`**: specific statements the reviewer should try to
   prove wrong, one per item, each testable on its own. "Two workers can
   never claim the same job" is good. "The code is correct" is not. A claim
   the reviewer couldn't test is untested, not true.
6. **`artifacts`**: the exact files and line ranges to read first, such as
   `src/jobs/worker.py:40-180`. Send paths, not file contents; the reviewer
   opens the files itself. This field does the most for the least effort: it
   turns "work out how my repository fits together" into "check these places".
7. **`mode`**: `review` (the default) asks for read-only work, with changes
   proposed as diffs. `implement` lets the reviewer edit files, and is only
   worth using with `zero-regression-surgical-implementation`.
8. **`notes`**: anything you want the reviewer to keep in mind, such as
   "prefer the smallest change". It's shown in its own section. Only request
   files passed with `--envelope` read it; through the MCP tool, put it in
   `context`.

## 3. What to put in the prompt

For a short question, a few sentences are enough. For a plan or a large
change, these sections help:

- **The goal and what you want back.** For example: "Review this plan before
  we build it. Tell us how it breaks, and where you would do it differently."
  Say that disagreement is welcome.
- **The user's words.** Quote the request or complaint as the user wrote it.
  Paraphrases lose the detail the reviewer needs.
- **What "done" means.** One line per requirement.
- **What can't change.** Only what is really fixed: an explicit user demand,
  a string a test depends on, a packaging constraint.
- **What is still open.** Every decision your assistant made on its own. See
  section 4.
- **The plan itself, in full.** Don't make the reviewer reconstruct it from
  a 4,000-line file.

## 4. Lock only what is really fixed

Never present the whole plan as fixed, and never present it as completely
open. Split it three ways:

- **Fixed**: only things you can't change, as listed above.
- **Open**: every decision your assistant made on its own authority. For
  each one, give the plan, the preferred option, the alternatives considered,
  how confident you are, and what would change your mind. That last item
  tells the reviewer where its attention changes the outcome.
- **Suggestions welcome**: say that improvements beyond the plan are fine,
  but ask for a short ranked list (five at most), each tied to a requirement.

If you mainly want an independent design rather than an attack on yours, use
`independent-design-second-opinion`. It compares options, recommends one and
lists the questions to put to the user.

## 5. What comes back

Wisp sends the reviewer one shared set of rules with every request, so every
answer has the same shape:

- the playbook's own sections first (for plan hardening, for example: a
  premise table, a failure-mode list, requirements and a build order);
- then one block per finding, headed like `### F-001 · P1 · high · <category>`,
  with where it is, what's wrong, the evidence, a failure scenario, a fix and
  how to verify the fix;
- then a short "checked and cleared" list;
- and last, a verdict block that Wisp reads into `review_verdict`:

```
<<<WISP_VERDICT
verdict: PASS | PASS_WITH_FIXES | BLOCK
confidence: high | medium | low
summary: <one or two sentences>
counts: P0=<n> P1=<n> P2=<n> P3=<n>
must_fix: <finding IDs, or none>
WISP_VERDICT>>>
```

You can ask for extra sections in your prompt, such as a per-decision table
(keep mine / prefer the alternative / ask the user first) or a short list of
questions for the user. They appear before the findings. Don't ask for a
different final verdict: the verdict block always comes last, and a review
without it counts as incomplete.

## 6. After the review

Your assistant should go through every finding and mark it accepted or
rejected. A rejection needs evidence, such as a file and line, command
output or a recalculation; "I think it's fine" doesn't count. Every ID in
`must_fix` must end up either fixed or rejected with evidence. Alternatives
the reviewer recommended are adopted on their merits, and questions meant for
the user go to the user before any building starts. If the plan changes a
lot, send the changed parts for another review, and mention the earlier one.

[AGENTS.md](../AGENTS.md#5-ingestion--reconciliation-rules) has the exact
rules coding assistants follow.

## 7. Keeping requests efficient

- **Ask for opinions and checks together.** Design feedback and claim
  testing fit in one request with separate sections, so you only make one
  round trip.
- **Send the plan with the request.** Include the plan text in full so the
  reviewer reads it instead of reconstructing it.
- **Put limits on open questions.** One alternative per decision, five
  suggestions at most. That keeps the answer broad without letting it sprawl.
- **Prefer precise artifacts to many.** Twenty exact line ranges beat two
  hundred vague pointers.
- **Watch the size on Windows.** The request travels on the command line, and
  Wisp stops at 30,000 characters. Move long material into files and list
  them as artifacts.

## 8. A worked example

[`examples/delegation-case-study.json`](../examples/delegation-case-study.json)
is a complete request: a plan to add retry backoff to a job queue, the
context the reviewer needs (including a failed earlier attempt), three claims
to test, the exact files to read, a primary and a recommended playbook, and a
note asking for the smallest change. Try it with `--dry-run` to see exactly
what would be sent:

```bash
python tools/antigravity_bridge.py --envelope examples/delegation-case-study.json --dry-run
```

One rule for the `Skills/` folder: only put playbooks there. Every `.md` file
in it needs YAML front matter, and a single file without it breaks the whole
playbook registry until you remove it.
