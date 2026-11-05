# SPDX-License-Identifier: AGPL-3.0-or-later
"""Package the dependency-free Olith extension as an installable VSIX."""
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]


def package(destination: Path | None = None) -> Path:
    extension = ROOT / "extensions" / "olith-vscode"
    metadata = json.loads((extension / "package.json").read_text())
    destination = destination or ROOT / "dist" / "olith-vscode.vsix"
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = f'''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"><Metadata><Identity Language="en-US" Id="{escape(metadata['name'])}" Version="{escape(metadata['version'])}" Publisher="{escape(metadata['publisher'])}"/><DisplayName>Olith</DisplayName><Description xml:space="preserve">{escape(metadata['description'])}</Description><Tags>olith,local,AI</Tags><Categories>Machine Learning</Categories><GalleryFlags>Public</GalleryFlags><Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="{escape(metadata['engines']['vscode'])}"/><Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/><Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/></Properties><License>extension/LICENSE</License></Metadata><Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/><Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/><Asset Type="Microsoft.VisualStudio.Services.Icons.Default" Path="extension/{escape(metadata['icon'])}" Addressable="true"/><Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE" Addressable="true"/></Assets></PackageManifest>'''
    types = '''<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="css" ContentType="text/css"/><Default Extension="png" ContentType="image/png"/><Default Extension="svg" ContentType="image/svg+xml"/><Default Extension="md" ContentType="text/markdown"/><Default Extension="vsixmanifest" ContentType="text/xml"/><Default Extension="" ContentType="text/plain"/></Types>'''
    temporary = destination.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("extension.vsixmanifest", manifest)
        output.writestr("[Content_Types].xml", types)
        for name in ("package.json", "extension.js", "client.js", "activity.js", "webview.js", "README.md", "media/olith.svg", "media/olith-activity.svg", "media/olith.png", "media/chat.css", "media/chat.js", "media/markdown.js"):
            output.write(extension / name, "extension/" + name)
        output.write(ROOT / "LICENSE", "extension/LICENSE")
    temporary.replace(destination)
    return destination


if __name__ == "__main__":
    print(package())
