
"""
Jaenvtix Setup - Auto JDK/Maven bootstrapper for Maven (pom.xml) projects

Este script implementa um fluxo idempotente e detalhadamente documentado (por logs) para:

1) Descobrir projetos Maven (pom.xml) no workspace atual (pasta raiz e subpastas imediatas).
2) Descobrir a versão de Java a partir do pom.xml, nesta ordem:
   - properties > java.version
   - maven-compiler-plugin > configuration > release
   - maven-compiler-plugin > configuration > compilerVersion
   - toolchain no pom.xml (jdkToolchain.version)
3) Detectar SO e arquitetura e mapear uma distribuição JDK compatível.
   - Preferência a LTS da Oracle quando existir; fallback para OpenJDK/Temurin.
   - URLs mantidas em uma tabela central, fácil de manter.
4) Criar a estrutura de diretórios na HOME:
   ~/.jaenvtix/
       temp/
       jdk-8/ , jdk-11/ , jdk-17/ , jdk-21/ , jdk-25/
         <jdk-version...>/
         mvn-custom/
           bin/
             mvn[.cmd|.sh]
             mvnd[.exe]
           ...
5) Baixar e extrair JDK (com retry e backoff simples, fallback de mirrors).
6) Baixar e extrair Maven (com retry e backoff, fallback de mirrors) e apontá-lo para o JDK instalado.
7) Atualizar ~/.m2/toolchains.xml e ~/.m2/settings.xml de forma segura e idempotente.
8) Em workspaces com múltiplos projetos, tratar cada projeto independentemente, incluindo .vscode/settings.json para cada um.
9) Limpar a pasta temporária ~/.jaenvtix/temp ao final (ou reportar resíduos se houver falhas).

Observações:
- Sem pom.xml: não faz nada naquele projeto.
- Mensagens claras em qualquer falha, com ação sugerida.
- O script pode ser executado várias vezes sem quebrar o ambiente.

Uso:
  python jaenvtix_setup.py

Requisitos:
- Python 3.8+
- Permissão de escrita na HOME do usuário para criar ~/.jaenvtix e ~/.m2

"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    # xml.etree is sufficient here and avoids extra deps
    import xml.etree.ElementTree as ET
except Exception as e:
    print(f"[FATAL] Falha ao importar biblioteca XML: {e}")
    sys.exit(1)


# ==========================
# Configuração e Constantes
# ==========================

HOME = Path.home()
JAENVTIX_HOME = HOME / ".jaenvtix"
TEMP_DIR = JAENVTIX_HOME / "temp"
M2_DIR = HOME / ".m2"

# Maven versão default
DEFAULT_MAVEN_VERSION = "3.9.9"

# Mapeamento de preferências de distribuição por versão LTS.
# Manter aqui os links base por SO/arch e versão de Java. Fallbacks na ordem.
# Nota: URLs podem mudar com o tempo; mantenha esta tabela atualizada conforme necessário.

@dataclass(frozen=True)
class JdkDist:
    name: str
    url: str  # URL do artefato (arquivo .zip/.tar.gz)
    ext: str  # 'zip' ou 'tar.gz'


def oracle_latest_url(java_version: str, os_name: str, arch: str) -> Optional[Tuple[str, str]]:
    """Return the Oracle "latest" artifact URL for the requested major/OS/arch."""

    fragments: Dict[Tuple[str, str], Tuple[str, str]] = {
        ("windows", "x86_64"): ("windows-x64", "zip"),
        ("linux", "x86_64"): ("linux-x64", "tar.gz"),
        ("linux", "aarch64"): ("linux-aarch64", "tar.gz"),
        ("macos", "x86_64"): ("macos-x64", "tar.gz"),
        ("macos", "aarch64"): ("macos-aarch64", "tar.gz"),
    }
    fragment = fragments.get((os_name, arch))
    if not fragment:
        return None
    platform_fragment, ext = fragment
    url = (
        f"https://download.oracle.com/java/{java_version}/latest/"
        f"jdk-{java_version}_{platform_fragment}_bin.{ext}"
    )
    return url, ext


def oracle_latest_dist(java_version: str, os_name: str, arch: str) -> JdkDist:
    """Build a :class:`JdkDist` preconfigured with the Oracle latest URL."""

    result = oracle_latest_url(java_version, os_name, arch)
    if result is None:
        raise ValueError(f"No Oracle artifact mapping for {java_version} {os_name}/{arch}")
    url, ext = result
    return JdkDist(f"Oracle{java_version}", url, ext)


def temurin_latest_url(java_version: str, os_name: str, arch: str) -> Optional[Tuple[str, str]]:
    """Return the Temurin API endpoint that redirects to the latest GA build."""

    os_fragment = {
        "windows": "windows",
        "linux": "linux",
        "macos": "mac",
    }.get(os_name)
    arch_fragment = {
        "x86_64": "x64",
        "aarch64": "aarch64",
    }.get(arch)
    if not os_fragment or not arch_fragment:
        return None
    ext = "zip" if os_name == "windows" else "tar.gz"
    url = (
        "https://api.adoptium.net/v3/binary/latest/"
        f"{java_version}/ga/{os_fragment}/{arch_fragment}/jdk/hotspot/normal/eclipse"
    )
    return url, ext


def temurin_latest_dist(java_version: str, os_name: str, arch: str) -> JdkDist:
    """Build a :class:`JdkDist` pointing to Adoptium's evergreen binary endpoint."""

    result = temurin_latest_url(java_version, os_name, arch)
    if result is None:
        raise ValueError(f"No Temurin artifact mapping for {java_version} {os_name}/{arch}")
    url, ext = result
    return JdkDist(f"Temurin{java_version}", url, ext)


# Tabela: (java_version -> { (os, arch) -> [JdkDist, ...] })
JDK_URLS: Dict[str, Dict[Tuple[str, str], List[JdkDist]]] = {
    # Java 8
    "8": {
        ("windows", "x86_64"): [temurin_latest_dist("8", "windows", "x86_64")],
        ("linux", "x86_64"): [temurin_latest_dist("8", "linux", "x86_64")],
        ("macos", "x86_64"): [temurin_latest_dist("8", "macos", "x86_64")],
        ("macos", "aarch64"): [temurin_latest_dist("8", "macos", "aarch64")],
    },
    # Java 11
    "11": {
        ("windows", "x86_64"): [
            JdkDist(
                "Oracle11",
                "https://download.oracle.com/java/11/latest/jdk-11_windows-x64_bin.zip",
                "zip",
            ),
        ],
        ("linux", "x86_64"): [
            JdkDist(
                "Oracle11",
                "https://download.oracle.com/java/11/latest/jdk-11_linux-x64_bin.tar.gz",
                "tar.gz",
            ),
        ],
        ("macos", "x86_64"): [
            JdkDist(
                "Oracle11",
                "https://download.oracle.com/java/11/latest/jdk-11_macos-x64_bin.tar.gz",
                "tar.gz",
            ),
        ],
        ("macos", "aarch64"): [temurin_latest_dist("11", "macos", "aarch64")],
    },
    # Java 17
    "17": {
        ("windows", "x86_64"): [
            JdkDist(
                "Oracle17",
                "https://download.oracle.com/java/17/latest/jdk-17_windows-x64_bin.zip",
                "zip",
            ),
        ],
        ("linux", "x86_64"): [
            JdkDist(
                "Oracle17",
                "https://download.oracle.com/java/17/latest/jdk-17_linux-x64_bin.tar.gz",
                "tar.gz",
            ),
        ],
        ("linux", "aarch64"): [
            JdkDist(
                "Oracle17",
                "https://download.oracle.com/java/17/latest/jdk-17_linux-aarch64_bin.tar.gz",
                "tar.gz",
            ),
        ],
        ("macos", "x86_64"): [
            JdkDist(
                "Oracle17",
                "https://download.oracle.com/java/17/latest/jdk-17_macos-x64_bin.tar.gz",
                "tar.gz",
            ),
        ],
        ("macos", "aarch64"): [
            JdkDist(
                "Oracle17",
                "https://download.oracle.com/java/17/latest/jdk-17_macos-aarch64_bin.tar.gz",
                "tar.gz",
            ),
        ],
    },
    # Java 21
    "21": {
        ("windows", "x86_64"): [
            oracle_latest_dist("21", "windows", "x86_64"),
        ],
        ("linux", "x86_64"): [
            oracle_latest_dist("21", "linux", "x86_64"),
        ],
        ("linux", "aarch64"): [
            oracle_latest_dist("21", "linux", "aarch64"),
        ],
        ("macos", "x86_64"): [
            oracle_latest_dist("21", "macos", "x86_64"),
        ],
        ("macos", "aarch64"): [
            oracle_latest_dist("21", "macos", "aarch64"),
        ],
    },
    # Java 25 LTS
    "25": {
        ("windows", "x86_64"): [
            oracle_latest_dist("25", "windows", "x86_64"),
        ],
        ("linux", "x86_64"): [
            oracle_latest_dist("25", "linux", "x86_64"),
        ],
        ("linux", "aarch64"): [
            oracle_latest_dist("25", "linux", "aarch64"),
        ],
        ("macos", "x86_64"): [
            oracle_latest_dist("25", "macos", "x86_64"),
        ],
        ("macos", "aarch64"): [
            oracle_latest_dist("25", "macos", "aarch64"),
        ],
    },
}

# Maven URLs
MAVEN_URLS: Dict[str, Dict[str, str]] = {
    # version -> { os -> url }
    "3.9.9": {
        "windows": "https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.zip",
        "linux": "https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.tar.gz",
        "macos": "https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.tar.gz",
    }
}


# ==========================
# Utilidades
# ==========================

def log(msg: str) -> None:
    """Print messages and force an immediate flush to stdout."""

    print(msg, flush=True)


def detect_os_arch() -> Tuple[str, str]:
    """Detect and normalize the current operating system and architecture."""

    sys_os = platform.system().lower()
    if sys_os.startswith("win"):
        os_name = "windows"
    elif sys_os.startswith("darwin") or sys_os.startswith("mac"):
        os_name = "macos"
    elif sys_os.startswith("linux"):
        os_name = "linux"
    else:
        os_name = sys_os

    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        arch = machine
    # Restrição explícita: apenas x64 e ARM64 são suportados
    if arch not in ("x86_64", "aarch64"):
        log(f"[ERRO] Arquitetura não suportada: {arch}. Suportado apenas x64 e ARM (aarch64).")
    return os_name, arch


def ensure_dirs() -> None:
    """Create the ~/.jaenvtix directory structure required for downloads."""

    # 4) Estrutura de diretórios
    try:
        (JAENVTIX_HOME).mkdir(parents=True, exist_ok=True)
        (TEMP_DIR).mkdir(parents=True, exist_ok=True)
        for v in ("8", "11", "17", "21", "25"):
            (JAENVTIX_HOME / f"jdk-{v}").mkdir(parents=True, exist_ok=True)
        log(f"[OK] Estrutura base criada/validada em: {JAENVTIX_HOME}")
    except Exception as e:
        log(f"[ERRO] Falha ao criar estrutura em {JAENVTIX_HOME}: {e}")
        # Fallback: tentar dentro de HOME mesmo assim (já é HOME). Reportar ao usuário
        raise


def find_projects_with_pom(root: Path) -> List[Path]:
    """Return root and first-level directories that contain a pom.xml."""

    # 1) Presença de pom.xml: varre pastas raiz do workspace atual
    projects: List[Path] = []
    # verificar raiz
    if (root / "pom.xml").exists():
        projects.append(root)
    # varrer subpastas de 1º nível
    for child in root.iterdir():
        if child.is_dir():
            pom = child / "pom.xml"
            if pom.exists():
                projects.append(child)
    return projects


# ==========================
# POM Parsing
# ==========================

def ns_cleanup(tag: str) -> str:
    """Strip the ``{namespace}`` prefix used by ElementTree."""

    # remove namespace {..}tag
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def find_first_text(elem: Optional[ET.Element], path_parts: List[str]) -> Optional[str]:
    """Traverse a simple path ignoring namespaces and return the found text."""

    # Busca descendente por caminho simples, ignorando namespaces
    if elem is None:
        return None
    cur = elem
    for part in path_parts:
        found = None
        for ch in cur:
            if ns_cleanup(ch.tag) == part:
                found = ch
                break
        if found is None:
            return None
        cur = found
    return (cur.text or "").strip() if cur is not None else None


def parse_java_version_from_pom(pom_path: Path) -> Optional[str]:
    """Extract the target Java version from a pom.xml using common heuristics."""

    try:
        tree = ET.parse(str(pom_path))
        root = tree.getroot()
        # properties > java.version
        props = None
        for ch in root:
            if ns_cleanup(ch.tag) == "properties":
                props = ch
                break
        if props is not None:
            for p in props:
                if ns_cleanup(p.tag) == "java.version" and (p.text or "").strip():
                    v = (p.text or "").strip()
                    log(f"[INFO] java.version encontrado em properties: {v}")
                    return normalize_java_version(v)
        # maven-compiler-plugin config
        v = find_from_maven_compiler(root, ["configuration", "release"]) or \
            find_from_maven_compiler(root, ["configuration", "compilerVersion"])  # noqa: E501
        if v:
            log(f"[INFO] Versão obtida do maven-compiler-plugin: {v}")
            return normalize_java_version(v)
        # toolchain dentro do POM (não comum, mas suportado)
        v = find_first_text(root, ["build", "plugins", "plugin"])  # quick check
        # Busca específica por jdkToolchain/version
        v2 = find_in_toolchain_version(root)
        if v2:
            log(f"[INFO] Versão obtida de toolchain no pom.xml: {v2}")
            return normalize_java_version(v2)
        return None
    except Exception as e:
        log(f"[ERRO] Falha ao ler {pom_path}: {e}")
        return None


def find_from_maven_compiler(root: ET.Element, conf_path: List[str]) -> Optional[str]:
    """Look up compiler configuration values inside the maven-compiler-plugin."""

    # Procura plugin maven-compiler-plugin
    for build in root:
        if ns_cleanup(build.tag) != "build":
            continue
        for plugins in build:
            if ns_cleanup(plugins.tag) != "plugins":
                continue
            for plugin in plugins:
                if ns_cleanup(plugin.tag) != "plugin":
                    continue
                artifact_id = find_first_text(plugin, ["artifactId"]) or ""
                if artifact_id.strip() == "maven-compiler-plugin":
                    return find_first_text(plugin, conf_path)
    return None


def find_in_toolchain_version(root: ET.Element) -> Optional[str]:
    """Search toolchain configurations for declared Java versions."""

    # Busca aproximação por jdkToolchain/version
    # Alguns plugins (maven-toolchains-plugin) podem declarar dentro de build/plugins
    for build in root:
        if ns_cleanup(build.tag) != "build":
            continue
        for plugins in build:
            if ns_cleanup(plugins.tag) != "plugins":
                continue
            for plugin in plugins:
                if ns_cleanup(plugin.tag) != "plugin":
                    continue
                artifact_id = find_first_text(plugin, ["artifactId"]) or ""
                if artifact_id.strip() in ("maven-toolchains-plugin", "toolchains-maven-plugin"):
                    # procurar <jdkToolchain><version>17</version></jdkToolchain>
                    conf = None
                    for ch in plugin:
                        if ns_cleanup(ch.tag) == "configuration":
                            conf = ch
                            break
                    if conf is not None:
                        # procurar jdkToolchain/version
                        for ch in conf.iter():
                            if ns_cleanup(ch.tag) == "jdkToolchain":
                                v = find_first_text(ch, ["version"])  # type: ignore
                                if v:
                                    return v
    return None


def normalize_java_version(v: str) -> Optional[str]:
    """Normalize values such as ``1.8`` or ``17.0.9`` to their major version."""

    v = v.strip()
    # Mapear 1.8 -> 8
    if v.startswith("1."):
        parts = v.split(".")
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[1]
    # Aceitar qualquer maior inteiro (non‑LTS também)
    m = re.match(r"^(\d{1,2})", v)
    if m:
        return m.group(1)
    return None


# ==========================
# Download e Extração
# ==========================

def download_with_retries(url: str, dest: Path, attempts: int = 3, backoff: float = 1.5) -> bool:
    """Download a URL with retry attempts and exponential backoff."""

    import urllib.request
    last_err: Optional[Exception] = None
    for i in range(1, attempts + 1):
        try:
            log(f"[DOWN] Baixando (tentativa {i}/{attempts}): {url}")
            with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            log(f"[OK] Download concluído: {dest}")
            return True
        except Exception as e:
            last_err = e
            wait = backoff ** i
            log(f"[WARN] Falha no download: {e}. Retentando em {wait:.1f}s...")
            time.sleep(wait)
    log(f"[ERRO] Falha definitiva ao baixar {url}: {last_err}")
    return False


def extract_archive(archive_path: Path, dest_dir: Path) -> bool:
    """Extract ``.zip`` or ``.tar.gz`` archives to the destination folder."""

    try:
        if archive_path.suffix == ".zip":
            with zipfile.ZipFile(archive_path, 'r') as z:
                z.extractall(dest_dir)
        elif archive_path.suffixes[-2:] == [".tar", ".gz"] or archive_path.suffix == ".tgz":
            with tarfile.open(archive_path, 'r:gz') as t:
                t.extractall(dest_dir)
        else:
            log(f"[ERRO] Formato de arquivo não suportado: {archive_path}")
            return False
        log(f"[OK] Extração concluída em: {dest_dir}")
        return True
    except Exception as e:
        log(f"[ERRO] Falha ao extrair {archive_path}: {e}")
        return False


# ==========================
# Maven settings / toolchains
# ==========================

def ensure_m2_dirs():
    """Ensure the ~/.m2 directory exists."""

    M2_DIR.mkdir(parents=True, exist_ok=True)


def merge_toolchains(java_version: str, java_home: Path) -> None:
    """Create or merge toolchains.xml with the requested Java version entry."""

    ensure_m2_dirs()
    toolchains = M2_DIR / "toolchains.xml"
    template = (
        "<toolchains>\n"
        "  <toolchain>\n"
        "    <type>jdk</type>\n"
        "    <provides>\n"
        f"      <version>{java_version}</version>\n"
        "      <vendor>any</vendor>\n"
        "    </provides>\n"
        "    <configuration>\n"
        f"      <jdkHome>{java_home.as_posix()}</jdkHome>\n"
        "    </configuration>\n"
        "  </toolchain>\n"
        "</toolchains>\n"
    )

    if not toolchains.exists():
        toolchains.write_text(template, encoding="utf-8")
        log(f"[OK] Criado ~/.m2/toolchains.xml para Java {java_version}")
        return

    try:
        tree = ET.parse(str(toolchains))
        root = tree.getroot()
        # Verificar se já existe entrada da versão
        for tc in root:
            if ns_cleanup(tc.tag) != "toolchain":
                continue
            prov = None
            for ch in tc:
                if ns_cleanup(ch.tag) == "provides":
                    prov = ch
                    break
            if prov is None:
                continue
            v = find_first_text(prov, ["version"]) or ""
            if v.strip() == java_version:
                # atualizar jdkHome se necessário
                conf = None
                for ch in tc:
                    if ns_cleanup(ch.tag) == "configuration":
                        conf = ch
                        break
                if conf is not None:
                    home_node = None
                    for ch in conf:
                        if ns_cleanup(ch.tag) == "jdkHome":
                            home_node = ch
                            break
                    if home_node is None:
                        new = ET.SubElement(conf, "jdkHome")
                        new.text = java_home.as_posix()
                    else:
                        home_node.text = java_home.as_posix()
                    tree.write(str(toolchains), encoding="utf-8", xml_declaration=False)
                    log(f"[OK] Atualizado toolchains.xml para Java {java_version}")
                    return
        # se não encontrou, adicionar nova toolchain
        new_tc = ET.fromstring(template).find("toolchain")
        if new_tc is not None:
            root.append(new_tc)
            tree.write(str(toolchains), encoding="utf-8", xml_declaration=False)
            log(f"[OK] Adicionada nova toolchain para Java {java_version}")
    except Exception as e:
        log(f"[WARN] Falha ao mesclar toolchains.xml: {e}. Substituindo com entrada mínima.")
        toolchains.write_text(template, encoding="utf-8")


def ensure_settings_xml() -> None:
    """Create a default settings.xml when it is missing from ~/.m2."""

    ensure_m2_dirs()
    settings = M2_DIR / "settings.xml"
    if not settings.exists():
        # Criar settings básico, preservando possibilidade de merge futuro
        content = (
            "<settings xmlns=\"http://maven.apache.org/SETTINGS/1.0.0\"\n"
            "          xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\"\n"
            "          xsi:schemaLocation=\"http://maven.apache.org/SETTINGS/1.0.0\n"
            "                              https://maven.apache.org/xsd/settings-1.0.0.xsd\">\n"
            "  <profiles/>\n"
            "  <activeProfiles/>\n"
            "</settings>\n"
        )
        settings.write_text(content, encoding="utf-8")
        log("[OK] Criado ~/.m2/settings.xml padrão")
    else:
        log("[INFO] ~/.m2/settings.xml já existe; preservado")


# ==========================
# VS Code settings por projeto
# ==========================

def update_vscode_settings(project_dir: Path, java_home: Path, maven_bin: Path) -> None:
    """Update .vscode/settings.json with java.home and maven.executable.path."""

    vscode_dir = project_dir / ".vscode"
    vscode_dir.mkdir(parents=True, exist_ok=True)
    settings_file = vscode_dir / "settings.json"
    data: Dict[str, object] = {}
    if settings_file.exists():
        try:
            data = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    # Atualizar/mesclar chaves
    data["java.home"] = str(java_home)
    data["maven.executable.path"] = str(maven_bin)
    # Outras chaves podem ser mantidas
    settings_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"[OK] Atualizado {settings_file}")


# ==========================
# Instalação JDK/Maven
# ==========================

def select_jdk_dist(java_version: str, os_name: str, arch: str) -> List[JdkDist]:
    """Build the ordered list of JDK distributions to try for the given combo."""

    # Lista de candidatos: tabela estática + geração dinâmica para versões não mapeadas
    candidates: List[JdkDist] = []
    static_list = JDK_URLS.get(java_version, {}).get((os_name, arch))
    if static_list:
        candidates.extend(static_list)

    # Se não há estático suficiente, gerar candidatos dinamicamente
    if not candidates:
        # 1) Oracle LTS/non-LTS padrão "latest"
        oracle_arch = None
        if os_name == "windows" and arch == "x86_64":
            oracle_arch = "windows-x64"
        elif os_name == "linux" and arch in ("x86_64", "aarch64"):
            oracle_arch = f"linux-{'x64' if arch=='x86_64' else 'aarch64'}"
        elif os_name == "macos" and arch in ("x86_64", "aarch64"):
            oracle_arch = f"macos-{'x64' if arch=='x86_64' else 'aarch64'}"
        # Windows ARM não suportado pela Oracle (padrão) na maioria das versões
        if oracle_arch:
            ext = "zip" if os_name == "windows" else "tar.gz"
            candidates.append(JdkDist(
                f"Oracle{java_version}",
                f"https://download.oracle.com/java/{java_version}/latest/jdk-{java_version}_{oracle_arch}_bin.{ext}",
                ext,
            ))
        # 2) Temurin
        temurin_tag = None
        if os_name == "windows" and arch == "x86_64":
            temurin_tag = "x64_windows"
        elif os_name == "linux" and arch in ("x86_64", "aarch64"):
            temurin_tag = ("x64_linux" if arch == "x86_64" else "aarch64_linux")
        elif os_name == "macos" and arch in ("x86_64", "aarch64"):
            temurin_tag = ("x64_mac" if arch == "x86_64" else "aarch64_mac")
        if temurin_tag:
            ext = "zip" if os_name == "windows" else "tar.gz"
            candidates.append(JdkDist(
                f"Temurin{java_version}",
                f"https://github.com/adoptium/temurin{java_version}-binaries/releases/latest/download/OpenJDK{java_version}U-jdk_{temurin_tag}_hotspot.{ext}",
                ext,
            ))
        # 3) Amazon Corretto
        corretto_arch = None
        if arch in ("x86_64", "aarch64"):
            corretto_arch = ("x64" if arch == "x86_64" else "aarch64")
        if os_name in ("windows", "linux", "macos") and corretto_arch:
            ext = "zip" if os_name == "windows" else "tar.gz"
            candidates.append(JdkDist(
                f"Corretto{java_version}",
                f"https://corretto.aws/downloads/latest/amazon-corretto-{java_version}-{corretto_arch}-{os_name}-jdk.{ext}",
                ext,
            ))
    return candidates


def ensure_jdk(java_version: str, os_name: str, arch: str) -> Optional[Path]:
    """Install the requested JDK if needed and return the resulting JAVA_HOME."""

    jdk_base = JAENVTIX_HOME / f"jdk-{java_version}"
    jdk_base.mkdir(parents=True, exist_ok=True)

    # Detectar se já existe JDK extraído
    existing = None
    if jdk_base.exists():
        for ch in jdk_base.iterdir():
            if ch.is_dir() and (ch / "bin").exists():
                existing = ch
                break
    if existing:
        log(f"[OK] JDK já presente: {existing}")
        return existing

    candidates = select_jdk_dist(java_version, os_name, arch)
    if not candidates:
        log(f"[ERRO] Não encontrado JDK para combinação: SO={os_name} arch={arch} java={java_version}")
        return None

    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    last_err = None
    for dist in candidates:
        try:
            log(f"[INFO] Tentando JDK de {dist.name} para Java {java_version} ({os_name}/{arch})")
            archive_name = f"jdk-{java_version}-{dist.name}.{dist.ext.replace('.', '_')}"
            archive_path = TEMP_DIR / archive_name
            if not download_with_retries(dist.url, archive_path):
                raise RuntimeError("download_failed")
            # Limpar destino antes de extrair para evitar sobras de tentativa anterior
            for ch in jdk_base.iterdir():
                try:
                    if ch.is_dir():
                        shutil.rmtree(ch, ignore_errors=True)
                    else:
                        ch.unlink(missing_ok=True)
                except Exception:
                    pass
            if not extract_archive(archive_path, jdk_base):
                raise RuntimeError("extract_failed")
            # Detectar pasta JDK após extração
            extracted_home = None
            for ch in jdk_base.iterdir():
                if ch.is_dir() and (ch / "bin").exists():
                    extracted_home = ch
                    break
            if not extracted_home:
                subs = [p for p in jdk_base.glob("**/bin") if p.is_dir()]
                if subs:
                    extracted_home = subs[0].parent
            if extracted_home:
                log(f"[OK] JDK instalado com {dist.name} em: {extracted_home}")
                return extracted_home
            else:
                raise RuntimeError("jdk_home_not_found")
        except Exception as e:
            last_err = e
            log(f"[WARN] Falha com distribuidor {dist.name}: {e}. Tentando próximo...")
            continue

    log(f"[ERRO] Instalação JDK falhou após testar {len(candidates)} distribuidores. Último erro: {last_err}")
    return None


def ensure_maven(java_version: str, os_name: str) -> Optional[Tuple[Path, Path]]:
    """Install a Maven distribution tied to the selected JDK and return paths."""

    # Retorna (maven_home, maven_bin)
    jdk_base = JAENVTIX_HOME / f"jdk-{java_version}"
    mvn_custom = jdk_base / "mvn-custom"
    mvn_bin_dir = mvn_custom / "bin"
    mvn_bin_dir.mkdir(parents=True, exist_ok=True)

    # Se já existir mvn em bin, retornar
    mvn_exe = mvn_bin_dir / ("mvn.cmd" if os_name == "windows" else "mvn")
    if mvn_exe.exists():
        return mvn_custom, mvn_exe

    # Baixar Maven
    url = MAVEN_URLS.get(DEFAULT_MAVEN_VERSION, {}).get(os_name)
    if not url:
        log(f"[ERRO] Maven não suportado para SO {os_name}")
        return None
    archive = TEMP_DIR / f"apache-maven-{DEFAULT_MAVEN_VERSION}-bin.{('zip' if os_name=='windows' else 'tar.gz')}"
    if not download_with_retries(url, archive):
        return None

    # Extrair para mvn-custom
    tmp_extract = TEMP_DIR / f"maven-extract-{DEFAULT_MAVEN_VERSION}"
    if tmp_extract.exists():
        shutil.rmtree(tmp_extract, ignore_errors=True)
    tmp_extract.mkdir(parents=True, exist_ok=True)

    if not extract_archive(archive, tmp_extract):
        return None

    # Mover conteúdo para mvn-custom
    extracted_root = None
    for ch in tmp_extract.iterdir():
        if ch.is_dir() and ch.name.startswith("apache-maven-"):
            extracted_root = ch
            break
    if not extracted_root:
        log("[ERRO] Estrutura inesperada após extrair Maven.")
        return None

    # mover tudo para mvn-custom
    for item in extracted_root.iterdir():
        dest = mvn_custom / item.name
        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            else:
                dest.unlink(missing_ok=True)
        if item.is_dir():
            shutil.copytree(item, dest)
        else:
            shutil.copy2(item, dest)

    # Garantir executável
    if os_name == "windows":
        mvn_exe = mvn_bin_dir / "mvn.cmd"
    else:
        mvn_exe = mvn_bin_dir / "mvn"
        try:
            mvn_exe.chmod(0o755)
        except Exception:
            pass

    log(f"[OK] Maven instalado em: {mvn_custom}")
    return mvn_custom, mvn_exe


# ==========================
# Fluxo principal por projeto
# ==========================

def process_project(project_dir: Path) -> None:
    """Run the full bootstrap flow for a single Maven project."""

    pom = project_dir / "pom.xml"
    if not pom.exists():
        log(f"[SKIP] Sem pom.xml em {project_dir}; nada a fazer.")
        return

    log(f"[INFO] Processando projeto: {project_dir}")
    java_version = parse_java_version_from_pom(pom)
    if not java_version:
        log("[ERRO] Não foi possível autoconfigurar: versão Java não encontrada no pom.xml.")
        return

    os_name, arch = detect_os_arch()
    log(f"[INFO] SO/arch detectados: {os_name}/{arch}")
    if arch not in ("x86_64", "aarch64"):
        log("[ERRO] Arquitetura não suportada por este script. Apenas x64 e ARM64 (aarch64) são suportadas.")
        return

    ensure_dirs()

    jdk_home = ensure_jdk(java_version, os_name, arch)
    if not jdk_home:
        log("[ERRO] Instalação JDK falhou. Abortando para este projeto.")
        return

    maven_home, maven_bin = (None, None)
    mvn = ensure_maven(java_version, os_name)
    if mvn:
        maven_home, maven_bin = mvn
    else:
        log("[ERRO] Instalação do Maven falhou. Abortando para este projeto.")
        return

    # Toolchains e settings: se o projeto precisar toolchains, ainda assim manter settings.xml
    try:
        merge_toolchains(java_version, jdk_home)
    except Exception as e:
        log(f"[WARN] Falha ao configurar toolchains: {e}")

    try:
        ensure_settings_xml()
    except Exception as e:
        log(f"[WARN] Falha ao garantir settings.xml: {e}")

    # VS Code settings por projeto
    try:
        update_vscode_settings(project_dir, jdk_home, maven_bin)  # type: ignore[arg-type]
    except Exception as e:
        log(f"[WARN] Falha ao atualizar VS Code settings: {e}")


def cleanup_temp() -> None:
    """Remove leftover files from ~/.jaenvtix/temp and report failures."""

    # 10) Limpeza da pasta temporária
    leftovers: List[Path] = []
    try:
        if TEMP_DIR.exists():
            for ch in TEMP_DIR.iterdir():
                try:
                    if ch.is_dir():
                        shutil.rmtree(ch, ignore_errors=True)
                    else:
                        ch.unlink(missing_ok=True)
                except Exception:
                    leftovers.append(ch)
        if leftovers:
            log("[WARN] Resíduos temporários não removidos:")
            for p in leftovers:
                log(f"  - {p}")
        else:
            log("[OK] Pasta temporária limpa.")
    except Exception as e:
        log(f"[WARN] Falha ao limpar temporários: {e}")


def main() -> None:
    """Discover Maven projects under the current workspace and bootstrap them."""

    # Determinar diretório raiz do workspace a partir do CWD
    root = Path.cwd()
    log(f"[START] Jaenvtix Setup no workspace: {root}")

    projects = find_projects_with_pom(root)
    if not projects:
        log("[INFO] Nenhum pom.xml encontrado no workspace. Nada a fazer.")
        return

    for proj in projects:
        try:
            process_project(proj)
        except Exception as e:
            log(f"[ERRO] Falha ao processar projeto {proj}: {e}")

    # limpeza final
    cleanup_temp()
    log("[DONE] Concluído.")


if __name__ == "__main__":
    main()
