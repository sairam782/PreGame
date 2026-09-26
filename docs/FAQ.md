# Pregame: the problem, what we built, and FAQ

Fact-checked 26 Sep 2026, 15:30–15:50 EDT, against the code at `prep-harness` commit `39ae2ed` (counts and the
version-3 status updated at 15:52), the live Atlas
databases and the team's research report. Every number says what it counts and out of what.

## The problem

A financial advisor sees the same clients for years. Before each meeting they need to know what changed, in the
markets and in the client's life. AI assistants now write these prep notes and advisors use them, but AI memory often
misses when a fact changes, so last quarter's figure or a client's old preference gets repeated as if it were still
true. Letting the assistant improve itself adds a second risk: a change can look better on the cases it was tuned on
and fail on new ones, and a system that can reach its own test can fake a win.

## What we built

1. **Briefs the advisor can check.** Every claim in a brief cites the stored facts it draws on, and the approved
   compliance wording is inserted by code, word for word.
2. **Facts keep their dates.** Every fact is stored with the date it became true, and a newer fact replaces an older
   one, so a brief doesn't repeat last quarter's figure. When a client's file disagrees with itself, the brief turns
   it into a question for the client.
3. **It proposes its own fixes.** When briefs miss what clients ask, the system proposes one change to its own
   settings: which facts go into the brief, the writing instructions, the data sources or the safety checks.
4. **A test it can't see or rewrite.** A change is adopted only if the proposed version clearly beats the current
   version on unseen test meetings, without side effects. Any attempt to change the test questions or the grading is
   refused.
5. **People stay in charge, with a record.** Riskier changes also need a person's sign-off. Every step goes into a
   tamper-evident audit log in MongoDB, and any earlier version can be restored.

The results, and their limits, are in the answers below.

---

## The idea

**What is Pregame?**
Pregame writes prep briefs for a financial advisor before each client meeting. A brief covers what changed in the
world and in the client's life, why it matters to this household, what the client is likely to ask, what to say, and
what to watch out for. Every claim in the brief cites the stored facts it draws on; code checks that those facts exist
and are current, but not that the sentence reports them faithfully.
What makes Pregame unusual: when its briefs start missing things, it can propose a change to its own settings, test
it, and adopt it only if it wins. It cannot change the test questions, the grading code or the scoring rules. Today a
person, or the demo script, asks for each proposal; the system doesn't start the cycle by itself.

**Why financial advisors?**
Advisors see the same households for years, while the world around those clients keeps changing: interest rates,
markets, tax rules, retirement-account rules. The clients change too: a retirement date moves, a business sale closes,
a client who wanted growth turns cautious after a market drop. A brief that repeats last quarter's rate, or a client's
outdated preference, is wrong in a way that is easy to miss. It is a clear case of a task whose right answer drifts
over time.

**Is this a real problem?**
Advisors do use these assistants: over 98% of Morgan Stanley's advisor teams actively use its internal AI assistant,
according to a case study published by OpenAI, which supplies the model
([OpenAI](https://openai.com/index/morgan-stanley/)). So the risk is not that advisors stop reading; it is that they
keep reading and pass the assistant's mistakes on. And AI memory often misses changes: on HaluMem, a published
benchmark, six AI memory systems (add-ons that let an assistant remember earlier conversations) each missed between
37% and 94% of the changes they should have recorded ([HaluMem](https://arxiv.org/html/2511.03506)). When two stored
facts contradicted each other, the right move was to notice and ask; the best of eight memory systems did so in 50.4%
of those cases, according to a study not yet peer-reviewed
([SubtleMemory](https://arxiv.org/html/2606.05761v1)). These tests use long chat histories, not advisor client files;
we found no public test on real client files.

**What problem statement does it address?**
Problem statement one, Recursive Harnessing: a harness that updates its own rules, context policies, guardrails and
tool access. Pregame can change all four, under three levels of control: some changes are adopted automatically if
they win the test, some only after they win and a person signs off, and some are never allowed. In today's live runs
the proposer changed only what goes into the brief; the automated tests exercise the other three.

**What is a "harness", and why improve it instead of the model?**
The harness is everything around the model: which facts reach it, the instructions it follows, the data sources it
may use, and the checks its output must pass. We leave the model's weights alone. Harness changes are limited (one
setting at a time, within fixed bounds), readable, versioned and reversible, so a person can see exactly what changed
and undo it. A change inside a model's weights can't be inspected that way.

**Does Pregame give investment advice?**
No. A brief is written for the advisor, and the model is instructed never to tell the client what to buy or sell.
Safety checks back this up: they block advice-like phrasing, promised outcomes such as "guaranteed returns", and
reworded compliance disclosures. Approved disclosure wording is inserted by code, word for word, and the model is told
not to write disclosures itself. The checks are word patterns, so they are a backstop, not a guarantee (see "The
safety checks").

## How it works

**What happens in one cycle?**
1. Something changes. A scripted market or client event adds new facts.
2. A brief is written. Claude Sonnet writes a brief from the facts the current settings select. The brief must pass
   the safety checks before it is stored.
3. A simulated review happens. Claude Haiku plays the advisor and answers the client's questions using only the
   brief. Plain code marks each answer against the known right answers.
4. Feedback is filed. If the brief missed something that changed, a pre-written note from the simulated advisor is
   recorded.
5. A change is proposed. Claude Opus reads the advisor's feedback, the practice-meeting results (which questions were
   missed, and why), the current settings, the latest brief and its own past proposals, then proposes one change to
   one setting.
6. The change is tested. The test gate first screens the proposed version on the practice meetings, then compares it
   with the current version on unseen test meetings the proposer has never seen. Code decides whether it won; for the
   riskier kinds of change, a win only sends it to a person for sign-off.
7. The decision is recorded. An adopted change is written to MongoDB in one transaction, with an entry in a
   tamper-evident audit log. Rejections and refusals are logged too.

**Why three different models?**
Each role gets the model that suits it. Opus runs once per proposal, so it proposes changes. Sonnet writes the
briefs. Haiku, the fastest and cheapest, answers the client's questions many times per test. No model grades
anything: Haiku only answers, and plain code marks each answer right or wrong, so no model grades its own work.

**What exactly can the system change about itself?**
Four kinds of settings, each with its own rules:

| Setting | Examples | What it takes to change it |
|---|---|---|
| What goes into the brief | how far back it looks for market facts (3 to 365 days; client notes are always included), how many facts (3 to 30), which kinds of facts, whether facts about this client's own holdings go first, section order, how many likely questions (2 to 8) | adopted automatically if it wins the test |
| Data sources | switching a source off | adopted automatically if it wins the test |
| | switching a source on | must win the test and get a person's sign-off |
| Writing instructions | e.g. "lead with the change that moves the client's budget"; at most 8 instructions, each at most 300 characters | must win the test and get a person's sign-off |
| Safety checks | re-enabling a check a person had switched off, wording unchanged | no sign-off needed, but it must still win the test |
| | adding, removing, disabling, rewording or reordering a check (a new check can only reuse one of the four built-in check functions) | must win the test and get a person's sign-off |
| The test questions, the grader, the audit log, the test gate's code, the scoring rules | anything at all | never allowed: refused every time, before any scoring |

**Why can some changes go through automatically but not others?**
The risk is different. Choosing which facts go into a brief can't give the model new instructions or new data
sources; it only changes which already-approved facts it sees, and the test gate rejects a choice that makes briefs
worse. A writing instruction, a new data source or a changed safety check alters what the model is told or allowed to
do, so a person signs off on it. The sign-off covers a fingerprint of the exact change: if the change is edited after
testing, or any setting changed since the test, the sign-off no longer matches.

**What is the "fence"?**
The fence is the single MongoDB transaction that adopts a change. It advances the setting's version only if nothing
moved since the test, and it checks all four kinds of settings, not just the one being changed. In the same step it
stores the new version, appends the audit-log entry and closes the proposal. Either all of that happens or none of it
does. If two changes to the same setting race each other, exactly one is adopted and the other is marked out of date;
an opt-in test showed this on the real Atlas cluster. The fence assumes a single test-gate process (see the limits).

**Can a change be undone?**
Yes. Every version is kept. A rollback is a person's command, and it creates a new version that copies an old one by
id, so the history is never rewritten. Every stored brief also carries a receipt naming the exact settings versions
and facts it used.

## Is the improvement real?

**How do you know a change actually helps?**
The proposed and current versions are compared on unseen test meetings. These come from simulated months 4 to 6; the
proposer sees results only from the practice meetings, months 1 to 3. In each meeting a reader model answers the
client's questions from the brief alone, and code checks those answers against the known right answers.
First, the proposed version must not score lower than the current version on the practice meetings. Then it must pass
nine conditions on the unseen test meetings, all at once:
- Accuracy: its average share of questions answered correctly must be at least 5 percentage points higher.
- Consistency: its worst meeting must not score lower than the current version's worst, and the share of meetings
  where every repeat run scores at least 80% must not drop.
- No side effects: it must not add missed changes, false alarms (answers that repeat a false rumour or bring up news
  that doesn't affect this client), outdated facts, claims without a source, or safety-check failures.
- Size: the facts handed to the writer must not grow by more than 50%.
Riskier changes also need a person's sign-off after they pass.

**Why so many conditions? Isn't accuracy enough?**
A system that optimises one number learns to game it. Mentioning everything raises accuracy but buries the advisor in
noise, so a proposed version may not raise false alarms or grow the facts handed to the writer by more than 50%.
Buying accuracy with advice-like phrasing would get the brief blocked in real use, so safety-check failures count
too. In all three live runs, the first proposal was a broad "include more" change. It raised the share of unseen-test
questions answered correctly by 18 to 20 points and was rejected anyway, because it grew the facts handed to the
writer by 52%, over the 50% limit. In one run it also raised false alarms.

**What happened on real models?**
We ran the loop three times at once on Claude Sonnet, Haiku and Opus, on the retirement clients only. Across the three
runs the model proposed 9 changes: the test gate adopted 2 and rejected 7.
- Broad change: rejected in all three runs (see above).
- Narrow change (adding regulation and disruption facts, and putting the facts that affect this client first):
  adopted in two of the three runs, raising the share of unseen-test questions answered correctly from 72% to 94% in
  one and from 74% to 94% in the other. In the third run the test gate rejected the same change (72% to 94%) because
  the safety checks wrongly flagged compliant sentences (see "Did the checks work in the live runs?").
- A planted attempt to rewrite the test questions (written into the demo script, not by the model): refused in all
  three runs.
- Audit log: all 21 entries verified in each of the three runs.

**Couldn't that be luck?**
We measured the noise. The same unchanged version answered 72%, 72% and 74% of unseen-test questions correctly across
the three runs: about 2 points of spread, against gains of about 20 points. Each test also runs every unseen meeting
twice. But the test is small: 6 unseen test meetings for the retirement clients, 35 questions in all, from two
simulated households, so it detects only large effects. The fair description is "it won a small simulated test", not
"it is proven better".

**Why is the model's third change rejected?**
In two of the three runs, and in the stage replay, the model's third change gained under 3 points (94% to 97% of
unseen-test questions answered correctly), under the 5-point bar, so the test gate rejected it. The system doesn't
adopt changes too small to tell apart from noise. In the other run the third change was the narrow change, rejected
because of the safety-check false alarms. This matters as much as the adoptions: the test gate said no to 7 of the
model's 9 changes (2 of those 7 rejections were caused by the false alarms).

**Are the unseen test meetings truly independent?**
Partly. They come from later simulated months, so the proposer can't tune to them directly. But the same generator
writes them, so they test whether a change carries over to later months of the same simulated world, often with the
same values, not to real clients. Our own audit also found overlap: 17 of the 35 unseen test questions for retirement
clients use the same wording as a practice question, and 15 of those 17 also have the same right answer. Across all
three client types, 51 of 105 unseen questions repeat a practice question exactly. We list this as a limit.

**Who grades the grader?**
The grader is code, not a model: it checks whether an answer contains the right terms and avoids outdated values.
That avoids a model judging a model, but it has blind spots our audits measured. It had been marking some right
answers wrong, such as "73, up from 71" (it saw the old value) and "down 16" (it expected "16%"); we fixed those
before the live runs. It still has blind spots. On questions about news that doesn't affect the client, "unknown"
passes, while a correct answer that names the news in order to dismiss it fails. "It is not 73" passes when 73 is
right. A reader that answered "unknown" to every unseen test question would still get 14 of 35 right, so scores start
at about 40%, not zero. The gains are measured above that floor.

## What stops it cheating?

**Could the proposer just rewrite the test to make itself look good?**
No. The test questions, the grader, the scoring rules and the audit log are frozen. Any proposal that touches them is
refused before any scoring. The demo shows this with a scripted tampering attempt, filed in the proposer's name, to
rewrite the unseen questions "to match what the current brief already covers". The test gate refuses it. Our demo
script writes that attempt; the model did not choose to cheat.

**Can the proposer see the test?**
Not directly. It receives a copy of the permitted data: the advisor's feedback, the practice-meeting results
(including which practice questions it got wrong and why), its current settings, an excerpt of the latest brief, and
its own past proposals. It never gets a connection to the database, and the model runs with every tool switched off.
It never sees the unseen test meetings' own records, and never sees their scores as numbers.
Two honest caveats:
- It learns how each past proposal fared on the unseen test meetings: adopted or rejected, and in words why (for
  example "inside the margin" or "false alarms rose"), with the numbers blanked out. Over many cycles, that feedback
  could let it drift toward the test. The fixes are a fresh, never-used test set for final checks, and hiding those
  reasons from later prompts.
- About half the unseen questions repeat practice questions (see "Are the unseen test meetings truly independent?"),
  and it does see which practice questions it got wrong.

**Is that isolation enforced by the database?**
Not yet. Today it is enforced by our code inside one program, and everything connects as one database user that can
read and write every database. The next step is a separate MongoDB user for the proposer that can only read what it is
allowed to see, so the database enforces the boundary itself.

**What if the system adopts something harmful anyway?**
Three layers limit the damage:
- Riskier changes need a person's sign-off.
- Every change is versioned, and a person can roll it back by id.
- Every brief is checked again at the moment it is written: one that fails the safety checks is redrafted once, then
  blocked and logged rather than shown.

## The safety checks

**What do the safety checks look for?**
Statements that don't cite a stored fact; facts that newer facts have replaced; advice-like phrasing ("you should
sell…"); promised outcomes ("guaranteed returns", "risk-free", "built to protect your capital"); reworded versions of
the approved compliance disclosures.

**Did the checks work in the live runs?**
Not at first, and this is worth telling. Across the three live runs the checks raised 34 flags, on 26 distinct
sentences, and every one was a false alarm. They flagged careful, compliant sentences such as "Do not present it as a
guaranteed outcome", "If she asks about guaranteed income…" and "Put inflation of 3.00% a year next to those rates".
In one run this made the test gate reject two real improvements.
We caught it when run B rejected every change: a diagnostic agent re-ran the checks on every recorded sentence and found all 34 flags were false alarms. We fixed the checks. An independent reviewer from another model
family (GPT-5.6 Sol) then sent the fix back twice for being too loose, finding 6 and then 8 phrasings that slipped
through, such as "Not only are returns guaranteed, they are tax-free." We closed all 14; the final version has not
been re-reviewed. The 26 real sentences are now tests that must pass, and 26 promises and orders, including those 14
tricky phrasings, are tests that must still be caught.
We then re-graded run C, the run replayed on stage, under the corrected checks: its decisions stand, and none of its
briefs trips the final checks. Runs A and B can't be fully replayed under the new checks, which is why the stage uses
run C.

**So are the safety checks reliable now?**
They are a backstop, not a guarantee. The advice and promise checks are word patterns, and each review round found
new phrasings that slipped past. (The checks on sources and replaced facts are exact.) The right next step is a
model-based check, with the word patterns kept as a fast first filter.

## The banker book: a second test

**What is the banker book?**
A dataset our data teammate built: one banker, six fictional clients, and May to August 2026 of notes, trades, client
emails and a fee table, plus a few older notes going back to 2021. It includes 24 call preps written by the teammate's
scripted assistant: a small program, not an AI model, that copies a busy assistant's careless habits. No single mistake
was placed by hand; the teammate scripted three writers with fixed habits, and the mistakes follow from them:
- The banker is accurate.
- A junior writes "No changes" unless the client raises something, and on a joint account assumes the first-named
  holder decides.
- The assistant updates labels from the latest note only, quotes the fee from the last note that mentions one, and
  rewords the compliance disclosures a little more over time.
A hidden answer key lists every mistake (53 in the whole file, 33 of them in the 24 preps) and the 10 actions the file
called for: ask the client (5), flag for the banker (2), brief both account holders (3).

**What did Pregame score on it?**
Every column covers the same 24 call preps.

| Per 24 preps | Scripted assistant, no harness | Pregame's harness, code writes the prep (no AI model) | Claude Sonnet alone (run 1, run 2) | Claude Sonnet with the harness (run 1, run 2) |
|---|---|---|---|---|
| Preps with at least one mistake | 17 of 24 (71%) | 0 of 24 | 0 and 0 | 0 and 0 |
| Mistakes, all 24 preps together | 33 | 0 | 0 and 0 | 0 and 0 |
| A promise the rules forbid ("This portfolio is built to protect your capital") | 1 | 0 | 0 and 0 | 0 and 0 |
| Actions the file called for, done, out of 10 | 0 | 10 | 6 and 7 | 7 and 8 |
| Questions the file did not call for | 0 | 0 | 27 and 26 | 2 and 3 |

Counting note: 10 of the scripted assistant's 33 mistakes are the mildest kind of disclosure rewording. Without them,
14 of its 24 preps (58%) have a mistake.
The two Claude Sonnet columns come from a run at 15:17 to 15:24 today: Sonnet wrote all 24 preps twice without the
harness, from each client's raw records, and twice with it, from the harness's compiled client file, where code had
already worked out the current facts, the questions and the watch-outs. Two runs per side is little evidence. The one
clear difference is unneeded questions: about 26 without the harness against about 3 with it, per 24 preps, and that
mostly comes from the harness's code choosing the questions.
One later change: with the harness, Sonnet was told both account holders share the account, yet named a single
decision maker in most of those preps (0 and 1 of the 3 "brief both" actions). We made code write that line, as it
already writes the disclosures. Re-scoring the same recorded Sonnet outputs, with no new model calls, gives 10 and 10
of 10 actions. That change was made after seeing these results, so it is in-sample too.

**So what does the harness add here?**
On this small book, guarantees and focus, not accuracy. Claude Sonnet writing from the raw records made none of the
scripted assistant's mistakes (0 of 24 preps with a mistake, in each of 2 runs). The harness adds approved wording
inserted by code, every fact dated and sourced, and only the questions the file calls for. Its rules:
- Labels are dated: "panic seller" is kept as a dated observation with its source, and lapses after 90 days unless
  reconfirmed. When newer evidence disagrees, it becomes a question for the next call. On this book, the newer evidence
  (a July questionnaire saying "growth") did the work; the 90-day limit never came into play.
- Said versus did: "I only do index funds" is kept with its date and compared with the actual trades. Single-stock
  buys since then produce a question.
- Decision maker: a junior's assumption never overrides the banker's note or the client's own emails. When sources
  conflict on a joint account, both account holders are briefed.
- "No changes" never erases a plan: the note is recorded as a contact, not a fact. If it conflicts with what the
  client actually did, it's flagged for the banker.
- Fees: quoted only from the dated fee table, never from old notes.
- Disclosures: inserted by code, word for word.

**Which rule matters most?**
We switched the rules off one at a time on the code-written preps. The locked disclosures matter most: without that
one rule, 13 of 24 preps have a mistake (19 reworded disclosures and the forbidden promise). The fee rule and the
said-versus-did rule each prevent 3 mistakes; the rule that a banker's note outranks a junior's assumption prevents 2.
Two rules change nothing on this data (label expiry, and "No changes" is not a fact), because other rules already
cover those cases here. With every rule off, 16 of 24 preps have a mistake.

**A perfect score sounds too good. Is it?**
It is narrower than it looks. We wrote the fixes from the answer key's list of mistakes and scored them on the same 24
preps, so this is an in-sample result. In the 0 of 24 column no AI model is involved: code writes the whole prep. It
shows the harness can apply these fixes using only the visible data. It does not show that the fixes carry over to
new clients.
An independent reviewer (GPT-5.6 Sol) confirmed that the harness never reads the answer key, and agreed this is an
in-sample result. A second review judged our first description of the Claude Sonnet comparison unfair: on the harness
side, code had already done most of the work and added the disclosures itself, so the model mostly formatted it. We
reworded the comparison to say so.
The real test is our teammate's version 3, now in Atlas in two sizes: 20 clients (73 preps; 9 clients held back as
unseen test clients) and 80 clients (291 preps). Claude Sonnet is writing those preps with and without the harness,
with our rules unchanged from the 6-client book; they are stored unscored until the scorer is updated for version 3
and reproduces its answer key. No version-3 number is quoted until then.

**How do you know the harness doesn't peek at the answer key?**
Within the harness, only a separate scoring program (the teammate's, run as its own process) reads the answer key.
The harness gets back only that program's summary totals, never per-prep results. The harness's code never names the
hidden data folder, and mentions the answer-key database only in the list of places it refuses to write. The command
that stores results writes only into databases whose names start with "pregame".
Two limits:
- The separation covers the running code. The people and agents who wrote the fixes did read the answer key's list of
  mistakes, which is why the result is in-sample.
- The team's read-only viewer shows the answer key to the audience behind a presenter switch. It writes nothing, and
  the harness never uses it.

## Why MongoDB?

**Is MongoDB central, or just storage?**
It's central. The self-improvement loop is a sequence of state changes that must be exact:
- Facts are stored with the date they became true, so the system can ask what was true on any past date.
- Settings are versioned documents with a pointer to the current version.
- The fence is a multi-document transaction.
- Rule enforcement: validators check the shape of every proposal, version and audit-log entry, and unique indexes make
  duplicate proposals and forked audit logs impossible.
- Score cache: scores are cached, keyed to the exact settings, the models and a fingerprint of the grading code. A
  current version is never re-scored needlessly, and a changed grader never reuses stale scores.

**Why a document database for this?**
The things being versioned have different shapes: a policy is a set of bounded numbers and lists, a rule set is a list
of short texts, and a safety check is a named check with a description. All four kinds of settings live in one
versioned collection in their natural shapes, and one transaction changes the settings, the audit log and the
proposal together. Other databases have transactions and schema checks too; we don't claim only MongoDB could do
this. We don't use Atlas Search, Vector Search or change streams.

**What is the audit log, and what does "tamper-evident" mean here?**
Every event, proposal, test, adoption and refusal is appended to a log in which each entry contains a fingerprint (a
hash) of the previous entry. Changing what an entry records (what happened, who did it, its details or its simulated
time), reordering entries, or removing one in the middle breaks the chain, and the check detects it.
We're clear about its limits: entries name briefs and settings by id, not by content, so editing a stored brief
directly would not break the chain; the chain has no secret key, so someone with write access could rebuild it;
deleting the newest entries goes unnoticed. Content fingerprints in each brief's receipt, plus a copy of the latest
fingerprint kept outside the database, would close these gaps.
To check the chain yourself, read dates as UTC, as the app does; a default MongoDB client reports a false mismatch at
entry 1.

**Did you test against real Atlas, not just a simulator?**
Yes, on the hackathon's Atlas Sandbox cluster. Most tests use an in-memory stand-in for speed (it skips the
validators). Five opt-in tests run on the real cluster and check that: a failure inside the fence leaves nothing
half-written; two racing changes to the same setting produce exactly one version; validators reject malformed
documents; unique indexes reject duplicates; timestamps survive the round trip.
The last test found a real bug the stand-in had hidden: audit-log entries written with a non-UTC time failed
verification after being read back. It's fixed, and it didn't affect the recorded runs, whose times were already UTC.

## How it was built

**Was all of this built today?**
Yes. All the code in the repository was written during the event: its history starts at 10:52 with an empty scaffold
and had 87 commits by 15:52. We brought reading (research notes on self-improving systems, MongoDB notes) and our own
tools for running and reviewing AI agents, not product code.

**Who built it?**
The team directed a set of AI agents. Claude agents wrote the code in parallel. Each core module was checked by a
different model family (OpenAI Codex) before it counted as done: 30 check reports, 14 of which sent work back. Later
work (the grader fixes, the safety-check fixes, the banker book) was reviewed by GPT-5.6 Sol: 7 reports, 3 of which
rejected the work. Whole-system audits also ran in parallel at 13:15 from eight angles: data flow; a red team playing a
malicious proposer; the grader; replay safety; the demo; claims versus code; unnecessary code; tests. Later changes
were reviewed one at a time. Every check and audit report we filed is in the repository, including the ones that sent
work back.

**How much testing is there?**
About 600 automated tests (595 when counted at 15:52), plus 5 opt-in tests that run only against the
real Atlas cluster. About 40 of them need the data teammate's banker-book files and skip without them. The tests include the 26 real sentences the
safety checks wrongly flagged, and 26 promises and orders they must still catch, including every phrasing the
independent reviewer found.

## The demo

**Is the demo live?**
What you see replays a live run on Claude Sonnet, Haiku and Opus, recorded today, graded with the safety checks as
corrected this afternoon; the run's decisions are unchanged. A first cycle makes 73 model calls, and the whole recorded
run, three cycles, made 153 calls and took 20 to 30 minutes. That is too slow, and too dependent on the network, for a
three-minute slot.
The replay uses the recorded model answers, never new ones. Each answer is looked up by a fingerprint of the exact
prompt, so a change that alters any prompt stops the replay with an error instead of letting it improvise. Grading
code isn't part of any prompt, so a replay grades the recorded answers with today's checks: that is how we re-graded
this run after fixing the safety checks. The database, the test gate, the transaction and the audit log all run for
real during the replay.

**Can I watch a live run?**
Yes. When connected to Atlas, the live page and the team's viewer re-read the database every few seconds, so they
update as a run happens. A full run takes 20 to 30 minutes; we can start one after the demo.

**How much does a cycle cost?**
A first cycle is 73 model calls. Each version, current and proposed, is scored on the 6 practice meetings once (12
calls: Sonnet writes 6 briefs, and Haiku answers from each) and on the 6 unseen test meetings twice (24 calls). Opus
writes the proposal (1 call). Later cycles cost less, because the current version's scores are cached: in the
recorded run the second and third cycles took 37 calls each. Today's runs used a Claude subscription through the
command-line tool rather than paid API credits. We didn't record token counts, so we have no measured dollar cost.

## Limits and next steps

**What are the main limits?**
- Simulated data: the markets, clients and questions are simulated. Simulated clients are easier than real ones.
- One client type on real models: only the retirement clients ran live, in 3 runs.
- Small tests: each proposal is judged on 6 unseen test meetings (35 questions for retirement clients), each run
  twice, enough to detect only large effects.
- A simple grader: it checks for words, not meaning, and 51 of the 105 unseen test questions repeat a practice
  question exactly, right answer included.
- In-process isolation: the proposer is kept from the test by our code, not by database permissions; everything
  connects as one database user.
- Limited feedback: the proposer sees whether each past proposal was adopted and, in words, which test-gate conditions
  it failed, with the numbers hidden. Over many cycles that could let it drift toward the unseen test meetings.
- Safety checks: the advice and promise checks are word patterns, and the final version has not been independently
  re-reviewed.
- Sign-off proves what was approved, not who approved it: it records the exact change's fingerprint and the
  computer's login name.
- The banker-book result is in-sample: the rules were written from the answer key and scored on the same 24 preps. Its
  "no harness" baseline is a scripted assistant; Claude Sonnet writing from the raw records made none of those
  mistakes either (0 of 24 preps, in each of 2 runs).
- One test-gate process: the fence assumes a single test-gate process. With two running at once, changes to different
  settings could both pass without being tested together.

**What would you build next?**
A separate, restricted database user for the proposer; a fresh, never-used test set for final sign-off, and hiding the
test gate's reasons from the proposer's later prompts; a model-based safety check behind the word patterns; a blind
check of the grader by a person; the teammate's 20-client banker book, with 9 clients held out; content fingerprints in
each brief's receipt, and a copy of the latest audit-log fingerprint kept outside the database; automatic rollback when
a live measure breaks after adoption.

**What does the research say about self-improving systems?**
- **Memory systems miss updates.** On HaluMem, a published benchmark, six AI memory systems each missed between 37% and
  94% of the changes they should have recorded, and recorded a wrong value for at most 1.15% of them
  ([HaluMem](https://arxiv.org/html/2511.03506)). So they fail mostly by keeping the old fact.
- **Designs that track dates.** The usual setup, which looks up stored text and answers from it, gave an outdated
  value 15% to 40% of the time across six tests of changing facts. A design that stores the dates each fact was true,
  and replaces an old value by rule, cut that to about 0% ([MemStrata](https://arxiv.org/abs/2606.26511)). This is a
  single-author study, not yet peer-reviewed.
- **Self-improving loops.** Loops that keep a change only after it passes a separate test typically gained about 2 to
  10 points on unseen tasks when scored by their own authors (for example +9 to +10 in
  [AutoSaddler](https://arxiv.org/html/2608.23041) and +3 to +5 in [ModularRSI](https://arxiv.org/html/2609.14857),
  both not yet peer-reviewed). When other groups re-tested such methods, gains were typically +1 to +3 points and
  sometimes below zero; one re-test found +0.6 ([Rethinking Harness Evolution](https://arxiv.org/html/2607.12227)).
  About one setting in four got worse: in a study by Amazon and NYU researchers, 4 of 16 settings did worse on unseen
  tasks than before the loop ran ([Ding et al.](https://wenwen-d.github.io/blog/harness-delta-attribution), a blog
  post, not a paper). These ranges are our research notes' reading of about 40 published results, not a formal review.
That is why Pregame treats every proposed change as a hypothesis to be tested, not a step forward to be assumed. It is
also why we keep two claims separate: "a time-aware harness beats a naive one" (the banker book, where the naive side
is a scripted assistant) and "the harness improves itself safely" (the live loop).

**Is this production-ready?**
No. It is a working prototype that shows the shape of a safe self-improving harness: small versioned changes, a test
the proposer can't see, a test gate that says no more often than yes, a person's sign-off where risk is higher, and a
checkable record of every step. Real clients, a larger test set and database-enforced isolation come before any real
advisor relies on it.
