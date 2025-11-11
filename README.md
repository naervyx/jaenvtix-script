# Jaenvtix Setup

Jaenvtix Setup is a single-file Python helper that automates provisioning of a
Maven development environment. It reads the Java requirements from one or more
`pom.xml` files, downloads a compatible JDK and Maven distribution, and wires
them into user-level tooling such as `~/.m2` and VS Code settings.

## Key features

- Scans the workspace root and direct subdirectories for Maven projects.
- Parses common `pom.xml` hints (`java.version`, `maven-compiler-plugin`,
  toolchains) to infer the required Java major version.
- Maps the host operating system/architecture to curated download URLs for
  Oracle, Temurin, and Amazon Corretto JDK distributions (including Oracle JDK
  21 and 25 "latest" endpoints).
- Installs Maven `3.9.9` inside a version-specific `~/.jaenvtix/jdk-<major>`
  folder, keeping the runtime self-contained per JDK.
- Creates or updates `~/.m2/toolchains.xml`, `~/.m2/settings.xml`, and
  `.vscode/settings.json` entries for each processed project.
- Cleans up temporary archives once provisioning completes.

## Requirements

- Python 3.8 or newer.
- Write access to the home directory in order to create `~/.jaenvtix` and
  `~/.m2`.
- Internet access for downloading JDK and Maven archives.

## Usage

From the repository (or any folder that contains `jaenvtix_setup.py`), run:

```bash
python jaenvtix_setup.py
```

The script prints progress logs to stdout. It is idempotent: re-running the
command will reuse existing installations unless newer archives must be
downloaded because the previous attempt failed.

## Download sources

Oracle JDK 21 and 25 entries use the official "latest" download URLs published
at [oracle.com/java/technologies/downloads/#jdk21](https://www.oracle.com/java/technologies/downloads/#jdk21)
and [oracle.com/java/technologies/downloads/#jdk25](https://www.oracle.com/java/technologies/downloads/#jdk25).
Fallback distributions for Temurin and Amazon Corretto rely on their respective
"latest" release endpoints.

## Customisation tips

- To add a new Java major version, extend the `JDK_URLS` mapping with the
  desired OS/architecture combinations. When an entry is absent the script will
  try to build URLs dynamically using the same distribution patterns.
- Adjust the `DEFAULT_MAVEN_VERSION` and `MAVEN_URLS` table if you require a
  different Maven release.
- The helper updates `.vscode/settings.json` without touching unrelated keys.
  Delete the generated file if you prefer to manage those settings manually.

## Troubleshooting

- Look for `[ERRO]` messages in the output to understand why a download or
  extraction failed. The script retries downloads automatically.
- If a custom antivirus or firewall blocks direct downloads, manually fetch the
  archives referenced in the logs and place them inside `~/.jaenvtix/temp`.
  Re-run the script afterwards; it will reuse the existing files.

