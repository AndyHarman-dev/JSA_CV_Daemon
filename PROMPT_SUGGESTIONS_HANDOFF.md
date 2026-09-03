# Suggested-replies prompt handover (CLI backends)

Structured-output backends (`anthropic`, `opencode-zen`, `mistral`, `openrouter`,
`opencode-go` `/chat`, `gemini`) already emit `suggested_replies` — the runtime-composed
structured-output contract in `jsa/pipeline/prompt_assembly.py::_structured_contract`
instructs them, and the JSON schema enforces the field. **Nothing further is required for
those backends.**

CLI backends (`claude-cli`, `google-cli`) only ever speak the sentinel grammar, and per
this project's convention (`CLAUDE.md` → "Prompt files"), the two prompt files under
`jsa/prompts/` are edited by you, never programmatically. To light up suggested-reply
chips for these backends, paste the block below into both `jsa/prompts/PROMPT_CDADJUST.md`
and `jsa/prompts/CVL_PROMPT.md`, near the existing sentinel-grammar instructions
(the section that already tells the model how to format `<<<NEED_INPUT>>>` blocks is the
natural place for it).

## Where to paste

Find the paragraph in each prompt file that explains the `<<<NEED_INPUT>>>` /
`<<<FINAL>>>` / `<<<END>>>` sentinel grammar, and add the following directly after it
(same section, not a new top-level heading):

## The exact block to paste

```
### Suggested replies

When you ask a clarifying question inside a `<<<NEED_INPUT>>>` block, you may optionally
follow your question with a `<<<SUGGESTIONS>>>` marker and then 2 to 4 short, distinct,
directly-sendable answers to that question, one per line, before the closing `<<<END>>>`.
These are shown to the user as clickable quick-reply buttons — they must be answers the
user could send as-is, not restatements of the question or generic acknowledgements
("yes"/"ok" are too vague; prefer concrete alternatives). Vary their length: include at
least one short, decisive option and at least one longer option that clarifies or pushes
back on your question. Do not pad the list to a fixed count — omit the block entirely if
you have nothing genuinely useful to suggest.

Example:

<<<NEED_INPUT>>>
Which dates should I use for your most recent role at Acme — the offer letter says
2019-present, but your CV draft says 2019-2023?
<<<SUGGESTIONS>>>
Use 2019-present, it's still my current role
Use 2019-2023, I left earlier this year
Let me check my offer letter and get back to you
<<<END>>>
```

## Why this is safe to paste

This is a **pure superset addition** to the sentinel grammar — `jsa/agents/protocol.py`'s
parser already treats an absent `<<<SUGGESTIONS>>>` block as `suggested_replies: None`
(today's behavior, unchanged), and only extracts a list when the marker is actually
present. Nothing else in either prompt file needs to change, and no other CLI-backend
behavior is affected. Until you paste this, CLI-backend jobs simply produce no suggested-
reply chips — they behave exactly as they do today.
