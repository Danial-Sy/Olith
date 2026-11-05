# SPDX-License-Identifier: AGPL-3.0-or-later
"""Built-in account shortcuts and official MCP setup presets."""

# Many apps use the same URL for login and the signed-in app. Those require the
# user to confirm the session instead of treating a successful page load as login.
WEBSITES = [
    ("notion", "Notion", "Work", "https://www.notion.so/login", "https://www.notion.so/", "Pages, databases, and workspace documents."),
    ("linear", "Linear", "Work", "https://linear.app/login", "https://linear.app/", "Issues, projects, and roadmaps."),
    ("trello", "Trello", "Work", "https://trello.com/login", "https://trello.com/", "Boards, cards, and checklists."),
    ("asana", "Asana", "Work", "https://app.asana.com/-/login", "https://app.asana.com/", "Tasks, projects, and team work."),
    ("clickup", "ClickUp", "Work", "https://app.clickup.com/login", "https://app.clickup.com/", "Tasks, documents, and spaces."),
    ("monday", "Monday", "Work", "https://auth.monday.com/", "https://auth.monday.com/", "Sign in to your workspace, then confirm the session."),
    ("airtable", "Airtable", "Work", "https://airtable.com/login", "https://airtable.com/", "Bases, records, and interfaces."),
    ("atlassian", "Atlassian", "Work", "https://id.atlassian.com/login", "https://start.atlassian.com/", "Jira, Confluence, and your Atlassian apps."),
    ("todoist", "Todoist", "Work", "https://app.todoist.com/auth/login", "https://app.todoist.com/", "Projects and personal tasks."),
    ("slack", "Slack", "Communication", "https://slack.com/signin", "https://app.slack.com/", "Channels, conversations, and workspace files."),
    ("discord", "Discord", "Communication", "https://discord.com/login", "https://discord.com/channels/@me", "Servers, channels, and direct messages."),
    ("telegram", "Telegram", "Communication", "https://web.telegram.org/", "https://web.telegram.org/", "Telegram Web. Complete phone or QR sign-in yourself."),
    ("whatsapp", "WhatsApp", "Communication", "https://web.whatsapp.com/", "https://web.whatsapp.com/", "WhatsApp Web. Link your device using its QR code."),
    ("proton", "Proton", "Communication", "https://account.proton.me/login", "https://mail.proton.me/", "Proton Mail and your Proton account."),
    ("dropbox", "Dropbox", "Files", "https://www.dropbox.com/login", "https://www.dropbox.com/home", "Cloud files and shared folders."),
    ("box", "Box", "Files", "https://account.box.com/login", "https://app.box.com/", "Files, folders, and shared content."),
    ("icloud", "iCloud", "Files", "https://www.icloud.com/", "https://www.icloud.com/", "iCloud apps available to your account."),
    ("gitlab", "GitLab", "Development", "https://gitlab.com/users/sign_in", "https://gitlab.com/dashboard/projects", "Repositories, issues, and pipelines."),
    ("bitbucket", "Bitbucket", "Development", "https://bitbucket.org/account/signin/", "https://bitbucket.org/", "Repositories, pull requests, and pipelines."),
    ("huggingface", "Hugging Face", "Development", "https://huggingface.co/login", "https://huggingface.co/settings/profile", "Models, datasets, and Spaces."),
    ("kaggle", "Kaggle", "Development", "https://www.kaggle.com/account/login", "https://www.kaggle.com/", "Datasets, notebooks, and competitions."),
    ("railway", "Railway", "Hosting", "https://railway.com/login", "https://railway.com/dashboard", "Projects, services, deployments, and logs."),
    ("vercel", "Vercel", "Hosting", "https://vercel.com/login", "https://vercel.com/dashboard", "Projects, deployments, and domains."),
    ("netlify", "Netlify", "Hosting", "https://app.netlify.com/login", "https://app.netlify.com/", "Sites, deploys, and team settings."),
    ("cloudflare", "Cloudflare", "Hosting", "https://dash.cloudflare.com/login", "https://dash.cloudflare.com/", "Domains, Workers, Pages, and account services."),
    ("supabase", "Supabase", "Hosting", "https://supabase.com/dashboard/sign-in", "https://supabase.com/dashboard/projects", "Databases, authentication, and project services."),
    ("render", "Render", "Hosting", "https://dashboard.render.com/login", "https://dashboard.render.com/", "Services, deployments, and logs."),
    ("sentry", "Sentry", "Monitoring", "https://sentry.io/auth/login/", "https://sentry.io/", "Errors, traces, and application monitoring."),
    ("figma", "Figma", "Design", "https://www.figma.com/login", "https://www.figma.com/files", "Design files, FigJam, and your workspace."),
    ("canva", "Canva", "Design", "https://www.canva.com/login/", "https://www.canva.com/", "Designs, presentations, and brand assets."),
    ("miro", "Miro", "Design", "https://miro.com/login/", "https://miro.com/app/dashboard/", "Boards, diagrams, and collaborative planning."),
]

BROWSER_CATALOG = {
    cid: {"label": label, "kind": "session", "category": category, "note": note,
          "sign_in_url": login, "check_url": home,
          "signed_in_host": home.split("/")[2], "confirmation_required": True}
    for cid, label, category, login, home, note in WEBSITES
}

# Provider requirements are visible during setup. These are configurations,
# not claims that an account is connected or its provider has approved this app.
MCP_PRESETS = [
    {"id": "railway", "label": "Railway", "url": "https://mcp.railway.com", "auth": "oauth", "category": "Hosting", "note": "Connect your Railway account to discover project, environment, and deployment tools. You can also reuse an installed Railway CLI's sign-in.", "docs": "https://docs.railway.com/ai/mcp-server"},
    {"id": "notion", "label": "Notion", "url": "https://mcp.notion.com/mcp", "auth": "oauth", "category": "Work", "note": "Search and work with pages and databases available to your account.", "docs": "https://developers.notion.com/guides/mcp/mcp"},
    {"id": "linear", "label": "Linear", "url": "https://mcp.linear.app/mcp", "auth": "oauth", "category": "Work", "note": "Issues, projects, and comments. Use /mcp/readonly on this host for read-only access.", "docs": "https://linear.app/docs/mcp"},
    {"id": "atlassian", "label": "Atlassian", "url": "https://mcp.atlassian.com/v2/mcp/", "auth": "oauth", "category": "Work", "note": "Jira and Confluence tools, subject to your organization's app policies.", "docs": "https://support.atlassian.com/atlassian-ai-gateway/docs/get-started-with-the-atlassian-remote-mcp-server"},
    {"id": "sentry", "label": "Sentry", "url": "https://mcp.sentry.dev/mcp", "auth": "oauth", "category": "Monitoring", "note": "Search errors and inspect issues in your Sentry projects.", "docs": "https://mcp.sentry.dev/"},
    {"id": "supabase", "label": "Supabase", "url": "https://mcp.supabase.com/mcp", "auth": "oauth", "category": "Hosting", "note": "Database and project tools. Narrow access using the provider's project and read-only URL settings.", "docs": "https://supabase.com/docs/guides/ai-tools/mcp"},
    {"id": "github", "label": "GitHub MCP", "url": "https://api.githubcopilot.com/mcp/", "auth": "bearer", "category": "Development", "note": "Connect with a GitHub personal access token. Your existing GitHub integration stays available separately.", "docs": "https://github.com/github/github-mcp-server"},
    {"id": "vercel", "label": "Vercel", "requires_client": True, "url": "https://mcp.vercel.com", "auth": "oauth", "category": "Hosting", "note": "Vercel requires an approved OAuth client ID or an HTTPS client metadata document URL identifying your app. A website login alone does not authorize these tools. See the provider setup instructions before connecting.", "docs": "https://vercel.com/docs/agent-resources/vercel-mcp"},
    {"id": "asana", "label": "Asana", "requires_client": True, "url": "https://mcp.asana.com/v2", "auth": "oauth", "category": "Work", "note": "Register an MCP OAuth app with Asana first and enter its client ID below.", "docs": "https://developers.asana.com/docs/integrating-with-asanas-mcp-server"},
    {"id": "slack", "label": "Slack", "requires_client": True, "url": "https://mcp.slack.com/mcp", "auth": "oauth", "category": "Communication", "note": "Requires a registered internal or Marketplace Slack app, its client ID and client secret, and workspace approval.", "docs": "https://docs.slack.dev/ai/slack-mcp-server/"},
]

# Verify CLI login with an authenticated API check, not saved credentials
# or a browser session.
CLI_PROVIDERS = {
    "google": {"label": "Google Workspace", "command": "gws", "package": "@googleworkspace/cli", "probe": ["drive", "files", "list", "--params", '{"pageSize":1,"fields":"files(id)"}'], "install": "npm install -g @googleworkspace/cli", "login": "gws auth login -s drive,gmail,sheets", "setup_note": "Requires a Google Cloud OAuth project/client configured first; follow the provider's setup instructions. Verification checks Drive read access. Grant only the services and scopes you need.", "docs": "https://github.com/googleworkspace/cli"},
    "microsoft": {"label": "Microsoft Graph", "command": "mgc", "probe": ["me", "get"], "install": "Install Microsoft Graph CLI using the provider instructions.", "login": "mgc login", "docs": "https://github.com/microsoftgraph/msgraph-cli"},
    "atlassian": {"label": "Atlassian Jira", "command": "acli", "probe": ["jira", "project", "list", "--limit", "1", "--json"], "install": "Install Atlassian CLI using the provider instructions.", "login": "acli jira auth login", "setup_note": "This CLI provides Jira tools. Choose the Atlassian MCP preset for Jira and Confluence together.", "docs": "https://developer.atlassian.com/cloud/acli/"},
    "vercel": {"label": "Vercel", "command": "vercel", "package": "vercel", "probe": ["whoami", "--format", "json", "--non-interactive"], "install": "npm install -g vercel", "login": "vercel login", "docs": "https://vercel.com/docs/cli"},
    "railway": {"label": "Railway", "command": "railway", "package": "@railway/cli", "probe": ["whoami", "--json"], "install": "npm install -g @railway/cli", "login": "railway login", "docs": "https://docs.railway.com/cli"},
    "netlify": {"label": "Netlify", "command": "netlify", "package": "netlify-cli", "probe": ["api", "getCurrentUser"], "install": "npm install -g netlify-cli", "login": "netlify login", "docs": "https://docs.netlify.com/api-and-cli-guides/cli-guides/get-started-with-cli/"},
    "cloudflare": {"label": "Cloudflare", "command": "wrangler", "package": "wrangler", "probe": ["whoami"], "install": "npm install -g wrangler", "login": "wrangler login", "docs": "https://developers.cloudflare.com/workers/wrangler/"},
    "supabase": {"label": "Supabase", "command": "supabase", "probe": ["projects", "list", "--output", "json"], "install": "Install the Supabase CLI using the provider instructions (global npm installation is unsupported).", "login": "supabase login", "docs": "https://supabase.com/docs/guides/local-development/cli/getting-started"},
    "github": {"label": "GitHub", "command": "gh", "probe": ["api", "user", "--jq", ".login"], "install": "Install GitHub CLI from https://cli.github.com/", "login": "gh auth login", "docs": "https://cli.github.com/manual/"},
    "gitlab": {"label": "GitLab", "command": "glab", "probe": ["api", "user"], "install": "Install glab using the provider instructions.", "login": "glab auth login", "docs": "https://docs.gitlab.com/cli/"},
    "huggingface": {"label": "Hugging Face", "command": "hf", "probe": ["auth", "whoami"], "install": "python -m pip install --upgrade huggingface_hub", "login": "hf auth login", "docs": "https://huggingface.co/docs/huggingface_hub/guides/cli"},
    "sentry": {"label": "Sentry", "command": "sentry", "probe": ["auth", "whoami", "--json", "--fresh"], "install": "Install the Sentry CLI using the provider instructions.", "login": "sentry auth login", "docs": "https://cli.sentry.dev/getting-started/"},
    "render": {"label": "Render", "command": "render", "probe": ["whoami", "--output", "json"], "install": "Install the Render CLI using the provider instructions.", "login": "render login", "docs": "https://render.com/docs/cli"},
    "kaggle": {"label": "Kaggle", "command": "kaggle", "probe": ["datasets", "list", "--mine", "--csv"], "install": "python -m pip install --upgrade kaggle", "login": "Authenticate the Kaggle CLI using the provider instructions.", "docs": "https://github.com/Kaggle/kaggle-cli"},
}

CLI_PRESETS = [
    {"id": cid + "-cli", "provider": cid, "label": spec["label"] + " CLI", "transport": "cli", "auth": "none",
     "docs": spec["docs"], "install": spec["install"], "login": spec["login"],
     "note": f"Use the official {spec['command']} CLI with its existing login. Install and sign in once in your terminal, then verify here. Website sessions are separate. " + spec.get("setup_note", "")}
    for cid, spec in CLI_PROVIDERS.items()
]


def preferred_tools(cid):
    if cid in CLI_PROVIDERS:
        return cid + "-cli"
    return cid if any(p["id"] == cid for p in MCP_PRESETS) else None
