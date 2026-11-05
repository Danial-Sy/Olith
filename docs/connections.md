# Connections in Olith

Open **Connections** in the sidebar. Use service tools or an official CLI when they
support the task. Use browser sessions for website access.

Services share two groups: **Connected** and **Disconnected**. Any working connection
puts a service in Connected: browser, CLI, MCP, GitHub account or live VS Code window.
CLIs do not need a browser login. Paused or offline connections move to Disconnected
unless another connection still works. Search and filters apply to both groups. Status
refreshes while the panel is open.

## Website sign-ins

The catalog contains 34 providers, including Google, Microsoft, GitHub, Railway, Notion,
Linear, Slack, Discord, Dropbox, GitLab, Figma, and others. Search by name or filter by
category. You can also add a custom website.

Built-in services use bundled logos that work offline. Custom connections use a generic
link icon.

Click **Browser sign-in**, continue from Olith's introduction page, and sign in yourself
in Olith's browser. Return to Connections. Olith checks the current page without
navigating away. If the URL cannot confirm login, click **Confirm browser session**
after you can see your account in that browser. **Check session** is available if
observation cannot detect your return. Turn on computer use to use the session in a
task. Check that login works with your account; providers can handle it differently.

Browser sign-in does not connect service tools. Its card says **Browser session**, with
a separate **Connect tools** action for providers that have a tool preset. Browser
startup and checks run in a worker so the rest of Olith stays responsive.

Forgetting a website removes its Olith record. Sign out in Olith's browser to end the
site's browser session.

## Direct service tools

Click **Connect tools** on a service card or **Connect service tools**. Providers with a
supported official CLI open that setup first; a working existing MCP connection is
preserved. The picker also offers the hosted MCP presets and custom MCP servers.

### Official CLIs

Connected cards show **CLI tools** at the top right when the CLI connection is verified
and enabled for tasks. **CLI paused** and **CLI not ready** distinguish disabled or
unverified connections. Website cards also show these badges alongside any **Browser
session** status. MCP connections use equivalent **MCP tools** badges.

On Linux, a system-wide npm install may fail with `EACCES`. npm recommends a user-owned
Node installation or npm directory; see its [permissions
guide](https://docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-packages-globally/).
If you choose a system-wide installation, the install command may need `sudo` (for
example, `sudo npm install -g vercel`). Run the provider login command and Olith as your
normal user, without `sudo`, so they share your user account's login. The setup dialog
includes provider-specific Linux permission guidance for npm CLIs.

CLI presets cover Google Workspace (`gws`), Microsoft Graph (`mgc`), Atlassian Jira
(`acli`), Vercel (`vercel`), Railway (`railway`), Netlify (`netlify`), Cloudflare
(`wrangler`), Supabase (`supabase`), GitHub (`gh`), GitLab (`glab`), Sentry (`sentry`),
Render (`render`), Hugging Face (`hf`) and Kaggle (`kaggle`).

Follow the provider installation instructions and sign in once in your own terminal.
Then choose **Verify CLI connection** in Olith. It checks the signed-in account or API
from an empty temporary folder, with a timeout and no interactive input. An installed
program or saved credential file alone does not count as connected. Missing programs,
failed login and timeouts are reported with setup instructions. Installed CLIs must be
on Olith's PATH or in a recognized user installation directory. Olith also checks common
npm, pip, Cargo and nvm install folders; restart Olith after changing its PATH.

Google Workspace additionally needs an OAuth Cloud project/client and selected scopes
configured with Google's instructions. Its check verifies Drive read access. Atlassian's
CLI provides Jira; choose its MCP preset for Confluence. Provider permissions and
administrator policies still apply.

Olith reuses credentials through the CLI and does not read its credential files or copy
tokens into the connection registry. Each connected CLI exposes a small help tool and a
command tool through `connection_tools`/`connection_call`. Commands use separate
arguments, a full project path and a timeout, with no interactive input. They ask for
approval unless Full access is enabled. Credential export, login/logout and
folder-override flags are refused. Help is read-only. Interactive shells, local
development servers and long-running streams should be run in a terminal instead. CLI
output is capped before it enters the prompt.

### MCP servers

Hosted MCP presets include Railway, Notion, Linear, Atlassian, Sentry, Supabase, GitHub
MCP, Vercel, Asana and Slack. A preset fills in setup details. Olith marks it connected
after checking the server and loading its tool list.

Hosted servers use Streamable HTTP, with OAuth, a bearer token, or no authentication.
OAuth supports PKCE, dynamic client registration when offered, and pre-registered client
IDs/secrets. The optional OAuth settings show the local callback URL to register with
providers that require it. Some providers require their own app registration,
administrator approval, or client allowlist. Asana and Slack's requirements are noted in
their presets.

Vercel requires an approved OAuth client ID or a client ID metadata document (CIMD)
hosted at an HTTPS URL. Enter that ID or document URL in OAuth app settings before
connecting through MCP. Its document must list Olith's displayed loopback redirect URL.
Olith does not have a publicly hosted client identity by default; signing into
vercel.com does not satisfy this requirement. See [Vercel's client identification
documentation](https://vercel.com/docs/agent-resources/vercel-mcp#client-identification-with-cimd).
Missing client settings are reported before trying to connect.

Local MCP servers use an executable and a JSON argument array. The program must already
be installed. Olith starts it on demand without a shell and closes it when the request
finishes. Put credentials in the secret environment field, not in arguments, which are
saved as configuration. Leave secret fields blank to keep saved values. Enter `{}` in
the environment field to clear its saved variables. Changing a server endpoint clears
the old credentials.

Tokens, OAuth client secrets, refresh tokens, and secret environment values live in the
operating system keyring. Credentials are not returned by the settings API. Unlock or
configure the keyring if sign-in cannot save them.

After **Save and connect**, use **Continue sign-in** if OAuth asks for it. The panel
reports the number of discovered tools. **Pause** removes the connection from Agentic
tasks. **Forget** removes configuration and saved credentials; you can also revoke
Olith's access in the provider's account settings.

## Railway

Choose **Railway**, then **Save and connect** and **Continue sign-in**. The [official
hosted MCP server](https://docs.railway.com/ai/mcp-server) connects at
`https://mcp.railway.com`. Discovered tools determine which project, environment, and
deployment operations the account can use.

Alternatively, install the [official Railway CLI](https://docs.railway.com/cli), run
`railway login`, and choose **Use an installed Railway CLI instead**. Olith configures
`railway mcp`, which reuses that CLI sign-in. Local source uploads still require the CLI
and access to the intended project directory.

The **Railway CLI** preset also provides general terminal operations. Like the other CLI
connections, it verifies the existing CLI login first.

## Vercel

Choose **Vercel CLI**, install `vercel` if needed, run `vercel login` in your own
terminal, then **Verify CLI connection**. This uses Vercel's official CLI login and does
not require registering Olith as a custom MCP OAuth client. Olith can then inspect
projects, deployments and logs, and perform approved CLI operations. Before project
operations, inspect the resolved owner and project from the intended directory with
`vercel project inspect --non-interactive`. Stop if the project needs linking or the
target differs; link only when explicitly requested. The separate **Vercel · MCP**
preset retains its client-identity requirements.

## Agent behavior and limits

Enabled, connected services add two small tools to the agent: `connection_tools` finds
relevant tools and their input schemas, and `connection_call` invokes one. A task does
not load every connected service's schemas into its initial prompt. Discovery returns up
to eight matching tool schemas, capped at 24,000 characters. Sub-tasks cannot use direct
service connections.

Read-only tools can run without a prompt. Other tools ask for approval, even in Shell
only mode. **Settings → Approval mode → Full access** skips shell, Git/GitHub and
service authorization prompts. It is off by default, shows a risk warning and adds an
indicator while enabled. Actions can be irreversible. File access has its own setting.

Olith checks the live tool definition before running it. A previous approval does not
apply after connection settings change. Results may be shortened, and non-text media is
left out. The client supports MCP tools. MCP resources, prompts, sampling and
elicitation are not supported yet.

MCP calls also use the project's before/after file snapshots. A local service tool that
edits project files invalidates checks from before that edit.

The client uses the official Python SDK v1.30 API. A small adapter saves validated OAuth
discovery metadata and token expiry across sessions. Dependencies install through
Olith's existing installer. No provider CLI or external server package is automatically
downloaded or launched on startup.
