# AI Assistant (`llm_assistant`)

Lets an LLM work with your Odoo data. It can look records up directly,
but it can only *propose* changes: nothing is created, changed or deleted
until the person it works for confirms the proposal in Odoo.

There are two ways to talk to it:

- **Odoo chat.** Users write to *AI Assistant* in Discuss, like they write to
  OdooBot. Odoo sends the conversation to your LLM server and runs the tools
  it asks for.
- **MCP.** External AI apps that speak the Model Context Protocol connect to
  `/llm_assistant/mcp` with an API key and use the same tools.

Both use the same tools and follow the same rules.

## How it works

```
Odoo chat:   user message -> queued answer -> cron -> LLM server <-> tools -> reply in the chat
MCP client:  JSON-RPC request with API key --------------------------> tools -> JSON result
                                                                         |
                                        propose_* tools create a proposal: the user opens it
                                        in Odoo and clicks Confirm (or Reject)
```

- **Tools** (`models/llm_assistant_tools.py`): `list_models`, `get_fields`,
  `search_records`, `count_records`, `group_records` read data;
  `propose_create`, `propose_update`, `propose_delete`, `propose_action`
  create proposals; `get_proposal` reports what happened to one.
- **Rights.** Every tool runs as the user the LLM works for, never as
  superuser, so the user's access rights and record rules apply. A proposal
  can only be confirmed by that same user, and the change runs with their
  rights.
- **Proposals** (*AI Assistant > Proposals*) show the records involved and a
  table of current and new values. When a proposal is confirmed, rejected or
  fails, a note is posted in the chat it came from.
- **Answers** in the Odoo chat are computed by the cron *AI Assistant: Answer
  Chat Messages*, one step (one LLM call plus the tools it asked for) at a
  time. Each answer and its full transcript is logged in *AI Assistant >
  Configuration > Answers*.

## Setup

1. Add the folder that contains this module to the addons path, e.g.
   `--addons-path=addons,custom_addons`, then install **AI Assistant**.
2. Run an LLM server with an OpenAI-compatible chat API and a model that
   supports tool calling. With [Ollama](https://ollama.com):

   ```bash
   ollama pull qwen3:8b      # any model with tool support works
   ollama serve              # listens on http://localhost:11434
   ```

   vLLM, llama.cpp (`llama-server --jinja`), LM Studio and hosted providers
   work too.
3. In *Settings > General Settings > AI Assistant*, set the server URL
   (`http://localhost:11434/v1` for Ollama), the model name and, if your
   server needs one, an API key. Click **Test Connection**.
4. Make sure Odoo runs cron workers (`--max-cron-threads`, 2 by default).
   With `--workers` > 0, each cron run is limited by `--limit-time-real-cron`;
   the assistant saves its work after each step, so a short limit only slows
   long answers down, but the limit must be longer than one LLM call (the
   *Timeout* setting, 120 s by default).

Other settings: *Max Steps* (rounds of tool calls per answer), *Instructions*
(added to the system prompt, e.g. house vocabulary) and *Blocked Models*.

## Using the Odoo chat

Open *AI Assistant > Chat*, or the *AI Assistant* conversation in Discuss.
Ask things like "Which customers have unpaid invoices over 1000?" or "Set
the phone of Azure Interior to +1 555 0199". Proposed changes appear as links
under the answer and in *AI Assistant > Proposals*.

## Connecting an MCP client

1. In your user preferences, open the *Security* tab, click **Create API
   Key** and choose the scope **AI Assistant (MCP)**. Keys with this scope
   only open the MCP endpoint: they cannot be used for Odoo's external API.
2. Point the client at the endpoint with the key as a bearer token:

   ```json
   {
     "mcpServers": {
       "odoo": {
         "url": "https://odoo.example.com/llm_assistant/mcp",
         "headers": {"Authorization": "Bearer YOUR_API_KEY"}
       }
     }
   }
   ```

   The endpoint implements the Streamable HTTP transport with JSON responses
   (no server-sent events) and the `tools` capability. For clients that only
   start local (stdio) servers, a bridge such as `mcp-remote` works:
   `npx mcp-remote https://odoo.example.com/llm_assistant/mcp --header "Authorization: Bearer YOUR_API_KEY"`.

Proposals made over MCP come back with a link; the user confirms them in
Odoo, and the client can check the outcome with `get_proposal`.

## Safety

- The assistant never reads `ir.config_parameter`, API keys, TOTP and passkey
  data or outgoing emails, and never proposes changes to users, groups,
  technical (`ir.*`) models or its own records. Fields whose name contains
  `password`, `token`, `secret` or `api_key` are never read, searched or
  written. *Blocked Models* adds your own patterns, e.g. `hr.employee*`.
- Records can contain text written by anyone (an email, a note, a product
  description), and an LLM can be tricked by instructions hidden in them.
  This is why writes need a confirmation: read each proposal before you
  confirm it.
- Chat history and tool results are sent to the configured LLM server. With
  a self-hosted server, they stay on your infrastructure.

## Extending

Add a tool by inheriting `llm.assistant.tools`: extend
`_get_tool_definitions()` with its name, description and JSON schema, and add
a `_tool_<name>` method that returns a JSON-serializable dict. The tool is
then available in the Odoo chat and over MCP.

## Limitations

- Answers arrive a few seconds after the question at best, since they are
  computed by a cron; there is no streaming.
- The assistant reads the last 20 messages of a chat, and cannot read
  attachments.
- Tools return at most 100 records per search; the LLM has to page or
  aggregate for more.

## Tests

```bash
./odoo-bin -d test_db --addons-path=addons,custom_addons -i llm_assistant \
    --test-enable --test-tags=/llm_assistant --stop-after-init
```

The tests mock the LLM server; no model is needed to run them.
