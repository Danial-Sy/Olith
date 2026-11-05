// SPDX-License-Identifier: AGPL-3.0-or-later
// Only bundled, known assets may appear in connection markup.
const CONNECTION_LOGOS = Object.freeze({
  google: "google.png", microsoft: "microsoft.svg", github: "github.svg", vscode: "vscode.svg",
  notion: "notion.svg", linear: "linear.svg", trello: "trello.svg", asana: "asana.svg",
  clickup: "clickup.svg", monday: "monday.png", airtable: "airtable.svg", atlassian: "atlassian.svg",
  todoist: "todoist.svg", slack: "slack.png", discord: "discord.svg", telegram: "telegram.svg",
  whatsapp: "whatsapp.svg", proton: "proton.svg", dropbox: "dropbox.svg", box: "box.svg",
  icloud: "icloud.svg", gitlab: "gitlab.svg", bitbucket: "bitbucket.svg", huggingface: "huggingface.svg",
  kaggle: "kaggle.svg", railway: "railway.svg", vercel: "vercel.svg", netlify: "netlify.svg",
  cloudflare: "cloudflare.svg", supabase: "supabase.svg", render: "render.svg", sentry: "sentry.svg",
  figma: "figma.svg", canva: "canva.svg", miro: "miro.svg",
});

function connectionIcon(connection) {
  const id = connection.transport === "cli" && connection.provider ? connection.provider
    : typeof connection.id === "string" && connection.id.endsWith("-cli") ? connection.id.slice(0, -4) : connection.id;
  const asset = Object.hasOwn(CONNECTION_LOGOS, id) ? CONNECTION_LOGOS[id] : "custom.svg";
  return `<img class="connection-logo" src="/static/assets/connections/${asset}" alt="" width="22" height="22" />`;
}
