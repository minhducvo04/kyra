# Plan P02: one app, not four pages (2026-09-16)

Duc's words: the functionality "seems primitive, and not elegant or well done"; make it "good like a
big app". This is the design pass that follows the integration of the four overnight branches, because
the pages it unifies live on different branches today. Claude Fable 5.1 plans and reviews; Codex builds.

## What a reader sees today (audit from tonight's screenshots)

1. Three visual systems. The HUD (`web/style.css`) is a dark cyan token set with a monospace voice. The
   loop page (`web/loop.css`) is a second dark palette with green accents and Inter. The Father page
   (`web/father.css`) is a light page with its own type. None imports another's tokens.
2. Four entry points with no navigation between them: `/`, `/loop`, `/father`, and the headset. The HUD
   header has five toggles (CONSOLE, FOCUS, SEARCH, TOOLS, JOBS) plus the backend switch; nothing links
   to `/loop`.
3. Marketing copy on a tool: the loop page opens with "TWO MODELS. VISIBLE CONTRIBUTIONS. A second pair of
   eyes." A working page states what it does in one line.
4. Six presence words in the HUD and on the headset, none on the loop page's cards; the same run status
   is spelled three ways across pages (badge, text, color).
5. Forms first, results second, on every page. The thing Duc looks at most (the latest answer, the task
   in review) sits below the form that created it.

## Decisions

- One token file, `web/tokens.css`: colours, type scale, spacing, radius, focus ring; imported first by
  every stylesheet. Dark by default; the Father page keeps its light theme through the same tokens with
  a `data-theme="light"` root, because Father reads in daylight and the choice was deliberate.
- One header component across `/`, `/loop`, `/father`: wordmark, page name, a small nav (Talk, Loop,
  Father when the tenant is father), the status dot. The HUD's five toggles become the CONSOLE plus three
  panel buttons; JOBS and TOOLS fold into CONSOLE's OPEN links in a later slice, not this one.
- Results above forms: latest contribution or task first, the form below or in a drawer.
- One status vocabulary: `queued, running, done, failed, needs you` as words, one badge style, one colour
  each, defined once in tokens and used by loop cards, Father cards and the console RUNS tab.
- No new framework, no build step, no new dependency. Plain CSS variables and the existing vanilla JS.

## Steps

1. [Red checks] `tests/test_design_tokens.py`: every page HTML links `tokens.css` first; no page CSS
   defines a hex colour outside `tokens.css`; every page carries the shared header markup; the five status
   words appear once as a shared class set -> verify: red on the commit that adds the file.
2. [Tokens and header, Codex] -> verify: tests green; screenshots of the three pages at 1280 and 375 px
   side by side, same header, same badges; zero console errors.
3. [Results first, Codex] loop and Father pages reorder; the HUD transcript unchanged -> verify: on each
   page the newest item is in the first viewport at 375 px without scrolling past a form.
4. [Copy pass, Claude then Codex] one sentence per page in place of the eyebrow and headline; the
   humanizer rule applies to nothing here because nothing leaves the machine -> verify: a reader who has
   never seen Kyra can say what each page does from its first line.
5. [Independent acceptance, Claude] browser run of every flow touched, phone and desktop, recorded in
   `docs/log/verification-history.md`.

## Not in this pass

Headset visuals (their own plan on the orb branch), new features, the CONSOLE absorbing JOBS and TOOLS,
any change to the loop's rules or the Father gates.
