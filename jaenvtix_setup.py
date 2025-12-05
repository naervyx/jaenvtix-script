from __future__ import annotations

import json
import platform
import re
import shutil
import sys
import tarfile
import time
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
import xml.etree.ElementTree as ET


"""Jaenvtix Setup - Bootstrap JDK and Maven for Maven workspaces."""

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = SCRIPT_DIR / "config"
HOME = Path.home()
JAENVTIX_HOME = HOME / ".jaenvtix"
TEMP_DIR = JAENVTIX_HOME / "temp"
M2_DIR = HOME / ".m2"
DEFAULT_MAVEN_VERSION = "3.9.11"


@dataclass(frozen=True)
class JdkDist:
    name: str
    url: str
    ext: str


@dataclass
class ProjectContext:
    project_dir: Path
    pom_path: Path
    java_version: Optional[str] = None
    os_name: Optional[str] = None
    arch: Optional[str] = None
    jdk_home: Optional[Path] = None
    maven_home: Optional[Path] = None
    maven_bin: Optional[Path] = None


class ValidationStep:
    description: str = ""

    def execute(self, ctx: ProjectContext) -> bool:
        raise NotImplementedError


class ValidationChain:
    def __init__(self, steps: List[ValidationStep]):
        self.steps = steps

    def run(self, ctx: ProjectContext) -> bool:
        for step in self.steps:
            log(f"[STEP] {step.description}")
            if not step.execute(ctx):
                log(f"[STOP] Step failed: {step.description}")
                return False
        return True


def oracle_latest_url(java_version: str, os_name: str, arch: str) -> Optional[Tuple[str, str]]:
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
    result = oracle_latest_url(java_version, os_name, arch)
    if result is None:
        raise ValueError(f"No Oracle artifact mapping for {java_version} {os_name}/{arch}")
    url, ext = result
    return JdkDist(f"Oracle{java_version}", url, ext)


def temurin_latest_url(java_version: str, os_name: str, arch: str) -> Optional[Tuple[str, str]]:
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
    result = temurin_latest_url(java_version, os_name, arch)
    if result is None:
        raise ValueError(f"No Temurin artifact mapping for {java_version} {os_name}/{arch}")
    url, ext = result
    return JdkDist(f"Temurin{java_version}", url, ext)


def corretto_latest_url(java_version: str, os_name: str, arch: str) -> Optional[Tuple[str, str]]:
    os_fragment = {
        "windows": "windows",
        "linux": "linux",
        "macos": "macos",
    }.get(os_name)
    arch_fragment = {
        "x86_64": "x64",
        "aarch64": "aarch64",
    }.get(arch)
    if not os_fragment or not arch_fragment:
        return None
    ext = "zip" if os_name == "windows" else "tar.gz"
    url = (
        "https://corretto.aws/downloads/latest/"
        f"amazon-corretto-{java_version}-{arch_fragment}-{os_fragment}-jdk.{ext}"
    )
    return url, ext


def corretto_latest_dist(java_version: str, os_name: str, arch: str) -> JdkDist:
    result = corretto_latest_url(java_version, os_name, arch)
    if result is None:
        raise ValueError(f"No Corretto artifact mapping for {java_version} {os_name}/{arch}")
    url, ext = result
    return JdkDist(f"Corretto{java_version}", url, ext)


DIST_BUILDERS = {
    "oracle_latest": oracle_latest_dist,
    "temurin_latest": temurin_latest_dist,
    "corretto_latest": corretto_latest_dist,
}


def load_jdk_urls(config_path: Path) -> Dict[str, Dict[Tuple[str, str], List[JdkDist]]]:
    if not config_path.exists():
        raise FileNotFoundError(f"Missing JDK URL configuration: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    mapping: Dict[str, Dict[Tuple[str, str], List[JdkDist]]] = {}
    for java_version, combos in data.items():
        mapping[java_version] = {}
        for combo_key, dist_names in combos.items():
            try:
                os_name, arch = combo_key.split("|", 1)
            except ValueError:
                log(f"[WARN] Invalid os|arch key in JDK config: {combo_key}")
                continue
            dists: List[JdkDist] = []
            for dist_name in dist_names:
                builder = DIST_BUILDERS.get(dist_name)
                if not builder:
                    log(f"[WARN] Unknown JDK distribution key: {dist_name}")
                    continue
                try:
                    dists.append(builder(java_version, os_name, arch))
                except Exception as exc:
                    log(f"[WARN] Failed to build JDK distribution {dist_name} for {combo_key}: {exc}")
            if dists:
                mapping[java_version][(os_name, arch)] = dists
    return mapping


def load_maven_urls(config_path: Path) -> Dict[str, Dict[str, str]]:
    if not config_path.exists():
        raise FileNotFoundError(f"Missing Maven URL configuration: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        data: Dict[str, Dict[str, str]] = json.load(handle)
    return data


JDK_URLS = load_jdk_urls(CONFIG_DIR / "jdk_urls.json")
MAVEN_URLS = load_maven_urls(CONFIG_DIR / "maven_urls.json")


def log(msg: str) -> None:
    print(msg, flush=True)


def detect_os_arch() -> Tuple[str, str]:
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

    if arch not in ("x86_64", "aarch64"):
        log(f"[ERROR] Unsupported architecture: {arch}. Only x64 and ARM64 are supported.")
    return os_name, arch


def ensure_dirs() -> None:
    JAENVTIX_HOME.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    for version in ("8", "11", "17", "21", "25"):
        (JAENVTIX_HOME / f"jdk-{version}").mkdir(parents=True, exist_ok=True)
    log(f"[OK] Base structure ready at: {JAENVTIX_HOME}")


def find_projects_with_pom(root: Path) -> List[Path]:
    projects: List[Path] = []
    if (root / "pom.xml").exists():
        projects.append(root)
    for child in root.iterdir():
        if child.is_dir():
            pom = child / "pom.xml"
            if pom.exists():
                projects.append(child)
    return projects


def ns_cleanup(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def find_first_text(elem: Optional[ET.Element], path_parts: List[str]) -> Optional[str]:
    if elem is None:
        return None
    current = elem
    for part in path_parts:
        found = None
        for child in current:
            if ns_cleanup(child.tag) == part:
                found = child
                break
        if found is None:
            return None
        current = found
    return (current.text or "").strip() if current is not None else None


def find_from_maven_compiler(root: ET.Element, conf_path: List[str]) -> Optional[str]:
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
                    configuration = None
                    for child in plugin:
                        if ns_cleanup(child.tag) == "configuration":
                            configuration = child
                            break
                    if configuration is not None:
                        for child in configuration.iter():
                            if ns_cleanup(child.tag) == "jdkToolchain":
                                version_text = find_first_text(child, ["version"])
                                if version_text:
                                    return version_text
    return None


def normalize_java_version(version: str) -> Optional[str]:
    version = version.strip()
    if version.startswith("1."):
        parts = version.split(".")
        if len(parts) >= 2 and parts[1].isdigit():
            return parts[1]
    match = re.match(r"^(\d{1,2})", version)
    if match:
        return match.group(1)
    return None


def parse_java_version_from_pom(pom_path: Path) -> Optional[str]:
    try:
        tree = ET.parse(str(pom_path))
        root = tree.getroot()
        properties = None
        for child in root:
            if ns_cleanup(child.tag) == "properties":
                properties = child
                break
        if properties is not None:
            for prop in properties:
                if ns_cleanup(prop.tag) == "java.version" and (prop.text or "").strip():
                    value = (prop.text or "").strip()
                    log(f"[INFO] java.version found in properties: {value}")
                    return normalize_java_version(value)
        compiler_version = find_from_maven_compiler(root, ["configuration", "release"]) or find_from_maven_compiler(root, ["configuration", "compilerVersion"])
        if compiler_version:
            log(f"[INFO] Version extracted from maven-compiler-plugin: {compiler_version}")
            return normalize_java_version(compiler_version)
        toolchain_version = find_in_toolchain_version(root)
        if toolchain_version:
            log(f"[INFO] Version extracted from pom toolchain: {toolchain_version}")
            return normalize_java_version(toolchain_version)
        return None
    except Exception as exc:
        log(f"[ERROR] Failed to read {pom_path}: {exc}")
        return None


def download_with_retries(url: str, dest: Path, attempts: int = 3, backoff: float = 1.5) -> bool:
    import urllib.request

    last_err: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            log(f"[DOWN] Downloading (attempt {attempt}/{attempts}): {url}")
            with urllib.request.urlopen(url, timeout=60) as response, open(dest, "wb") as handle:
                shutil.copyfileobj(response, handle)
            log(f"[OK] Download completed: {dest}")
            return True
        except Exception as exc:
            last_err = exc
            wait = backoff ** attempt
            log(f"[WARN] Download failed: {exc}. Retrying in {wait:.1f}s...")
            time.sleep(wait)
    log(f"[ERROR] Download failed after {attempts} attempts for {url}: {last_err}")
    return False


def extract_archive(archive_path: Path, dest_dir: Path) -> bool:
    try:
        if archive_path.suffix == ".zip":
            with zipfile.ZipFile(archive_path, "r") as archive:
                archive.extractall(dest_dir)
        elif archive_path.suffixes[-2:] == [".tar", ".gz"] or archive_path.suffix == ".tgz":
            with tarfile.open(archive_path, "r:gz") as archive:
                archive.extractall(dest_dir)
        else:
            log(f"[ERROR] Unsupported archive format: {archive_path}")
            return False
        return True
    except Exception as exc:
        log(f"[ERROR] Failed to extract {archive_path}: {exc}")
        return False


def ensure_m2_dirs() -> None:
    M2_DIR.mkdir(parents=True, exist_ok=True)


def merge_toolchains(java_version: str, java_home: Path) -> None:
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
        log(f"[OK] Created ~/.m2/toolchains.xml for Java {java_version}")
        return

    try:
        tree = ET.parse(str(toolchains))
        root = tree.getroot()
        for toolchain in root:
            if ns_cleanup(toolchain.tag) != "toolchain":
                continue
            provides = None
            for child in toolchain:
                if ns_cleanup(child.tag) == "provides":
                    provides = child
                    break
            if provides is None:
                continue
            version_text = find_first_text(provides, ["version"]) or ""
            if version_text.strip() == java_version:
                configuration = None
                for child in toolchain:
                    if ns_cleanup(child.tag) == "configuration":
                        configuration = child
                        break
                if configuration is not None:
                    home_node = None
                    for child in configuration:
                        if ns_cleanup(child.tag) == "jdkHome":
                            home_node = child
                            break
                    if home_node is None:
                        new_home = ET.SubElement(configuration, "jdkHome")
                        new_home.text = java_home.as_posix()
                    else:
                        home_node.text = java_home.as_posix()
                    tree.write(str(toolchains), encoding="utf-8", xml_declaration=False)
                    log(f"[OK] Updated toolchains.xml for Java {java_version}")
                    return
        new_toolchain = ET.fromstring(template).find("toolchain")
        if new_toolchain is not None:
            root.append(new_toolchain)
            tree.write(str(toolchains), encoding="utf-8", xml_declaration=False)
            log(f"[OK] Added new toolchain for Java {java_version}")
    except Exception as exc:
        log(f"[WARN] Failed to merge toolchains.xml: {exc}. Replacing with minimal entry.")
        toolchains.write_text(template, encoding="utf-8")


def ensure_settings_xml() -> None:
    ensure_m2_dirs()
    settings = M2_DIR / "settings.xml"
    if not settings.exists():
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
        log("[OK] Created default ~/.m2/settings.xml")
    else:
        log("[INFO] ~/.m2/settings.xml already exists; preserved")


def update_vscode_settings(project_dir: Path, java_home: Path, maven_bin: Path) -> None:
    def as_vscode_user_path(target: Path) -> str:
        home_posix = HOME.as_posix()
        target_posix = target.as_posix()
        if target_posix.startswith(home_posix):
            return target_posix.replace(home_posix, "${userHome}", 1)
        return target_posix

    vscode_dir = project_dir / ".vscode"
    vscode_dir.mkdir(parents=True, exist_ok=True)
    settings_file = vscode_dir / "settings.json"
    data: Dict[str, object] = {}
    if settings_file.exists():
        try:
            data = json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    java_home_path = as_vscode_user_path(java_home)
    maven_bin_path = as_vscode_user_path(maven_bin)
    user_settings_path = as_vscode_user_path(M2_DIR / "settings.xml")

    data.pop("java.home", None)
    data["java.jdt.ls.java.home"] = java_home_path
    data["java.jdt.ls.lombokSupport.enabled"] = True
    data["maven.executable.preferMavenWrapper"] = True
    data["maven.executable.path"] = maven_bin_path
    data["java.compile.nullAnalysis.mode"] = "automatic"
    data["java.configuration.updateBuildConfiguration"] = "automatic"
    data["java.configuration.maven.userSettings"] = user_settings_path

    settings_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"[OK] Updated {settings_file}")


def select_jdk_dist(java_version: str, os_name: str, arch: str) -> List[JdkDist]:
    candidates: List[JdkDist] = []
    static_list = JDK_URLS.get(java_version, {}).get((os_name, arch))
    if static_list:
        candidates.extend(static_list)

    if not candidates:
        if java_version not in {"8", "11", "17"}:
            try:
                candidates.append(oracle_latest_dist(java_version, os_name, arch))
            except ValueError:
                pass
        for factory in (corretto_latest_dist, temurin_latest_dist):
            try:
                candidates.append(factory(java_version, os_name, arch))
            except ValueError:
                continue
    return candidates


def _jdk_base(java_version: str) -> Path:
    return JAENVTIX_HOME / f"jdk-{java_version}"


def locate_existing_jdk(java_version: str) -> Optional[Path]:
    jdk_base = _jdk_base(java_version)
    if not jdk_base.exists():
        return None
    for child in jdk_base.iterdir():
        if child.is_dir() and (child / "bin").exists():
            return child
    return None


def download_jdk_package(java_version: str, os_name: str, arch: str) -> Optional[Tuple[Path, JdkDist]]:
    candidates = select_jdk_dist(java_version, os_name, arch)
    if not candidates:
        log(f"[ERROR] No JDK distribution found for OS={os_name} arch={arch} java={java_version}")
        return None

    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    last_err: Optional[Exception] = None
    for dist in candidates:
        try:
            log(f"[INFO] Attempting JDK download {dist.name} ({os_name}/{arch})")
            archive = TEMP_DIR / f"jdk-{java_version}-{dist.name}.{dist.ext}"
            if download_with_retries(dist.url, archive):
                return archive, dist
            raise RuntimeError("download_failed")
        except Exception as exc:
            last_err = exc
            log(f"[WARN] Failed to download {dist.name}: {exc}. Trying next candidate...")
    log(f"[ERROR] Unable to download JDK after {len(candidates)} attempts. Last error: {last_err}")
    return None


def _cleanup_old_jdk_content(jdk_base: Path) -> None:
    for child in jdk_base.iterdir():
        if child.name == "mvn-custom":
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except Exception:
            pass


def install_jdk_from_archive(java_version: str, archive: Path, dist: JdkDist) -> Optional[Path]:
    jdk_base = _jdk_base(java_version)
    jdk_base.mkdir(parents=True, exist_ok=True)

    _cleanup_old_jdk_content(jdk_base)
    if not extract_archive(archive, jdk_base):
        log("[ERROR] Failed to extract the downloaded JDK.")
        return None

    extracted_home: Optional[Path] = None
    for child in jdk_base.iterdir():
        if child.is_dir() and (child / "bin").exists():
            extracted_home = child
            break
    if not extracted_home:
        sub_bin_dirs = [path for path in jdk_base.glob("**/bin") if path.is_dir()]
        if sub_bin_dirs:
            extracted_home = sub_bin_dirs[0].parent

    if extracted_home:
        log(f"[OK] JDK installed with {dist.name} at: {extracted_home}")
        return extracted_home

    log("[ERROR] JDK structure not found after extraction.")
    return None


def install_jdk_with_fallback(
    java_version: str,
    os_name: str,
    arch: str,
    skip: Optional[Set[JdkDist]] = None,
) -> Optional[Path]:
    candidates = select_jdk_dist(java_version, os_name, arch)
    if skip:
        candidates = [dist for dist in candidates if dist not in skip]
    if not candidates:
        log("[ERROR] No remaining JDK candidates to try.")
        return None

    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    last_err: Optional[Exception] = None
    for dist in candidates:
        try:
            log(f"[INFO] Trying fallback JDK {dist.name} ({os_name}/{arch})")
            archive = TEMP_DIR / f"jdk-{java_version}-{dist.name}.{dist.ext}"
            if not download_with_retries(dist.url, archive):
                raise RuntimeError("download_failed")
            jdk_home = install_jdk_from_archive(java_version, archive, dist)
            if jdk_home:
                return jdk_home
            last_err = RuntimeError("install_failed")
            log(f"[WARN] Failed to install JDK with {dist.name}. Trying next candidate...")
        except Exception as exc:
            last_err = exc
            log(f"[WARN] Failed to prepare {dist.name}: {exc}. Trying next candidate...")

    log(
        "[ERROR] Unable to install JDK after testing "
        f"{len(candidates)} distributions. Last error: {last_err}"
    )
    return None


def locate_existing_maven(java_version: str, os_name: str) -> Optional[Tuple[Path, Path]]:
    jdk_base = _jdk_base(java_version)
    mvn_custom = jdk_base / "mvn-custom"
    mvn_bin = mvn_custom / "bin"
    mvn_executable = mvn_bin / ("mvn.cmd" if os_name == "windows" else "mvn")
    if mvn_executable.exists():
        return mvn_custom, mvn_executable
    return None


def _resolve_maven_distro(os_name: str) -> Optional[Tuple[str, str]]:
    url = MAVEN_URLS.get(DEFAULT_MAVEN_VERSION, {}).get(os_name)
    if not url:
        log(f"[ERROR] Maven is not supported for OS {os_name}")
        return None
    ext = "zip" if os_name == "windows" else "tar.gz"
    return url, ext


def _download_maven_distribution(url: str, archive: Path) -> bool:
    if archive.exists() and archive.stat().st_size > 0:
        log(f"[INFO] Reusing existing Maven artifact: {archive}")
        return True
    return download_with_retries(url, archive)


def download_maven_package(os_name: str) -> Optional[Tuple[Path, str]]:
    distro = _resolve_maven_distro(os_name)
    if distro is None:
        return None

    url, ext = distro
    archive = TEMP_DIR / f"apache-maven-{DEFAULT_MAVEN_VERSION}-bin.{ext}"
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    if _download_maven_distribution(url, archive):
        return archive, ext
    return None


def _extract_maven_archive(archive: Path) -> Optional[Tuple[Path, Path]]:
    extract_dir = TEMP_DIR / f"maven-extract-{DEFAULT_MAVEN_VERSION}"
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    if not extract_archive(archive, extract_dir):
        return None

    for candidate in extract_dir.iterdir():
        if candidate.is_dir() and candidate.name.startswith("apache-maven-"):
            return candidate, extract_dir

    log("[ERROR] Unexpected structure after extracting Maven.")
    return None


def install_maven_from_archive(
    java_version: str, os_name: str, archive: Path
) -> Optional[Tuple[Path, Path]]:
    jdk_base = _jdk_base(java_version)
    jdk_base.mkdir(parents=True, exist_ok=True)

    extracted = _extract_maven_archive(archive)
    if extracted is None:
        return None
    extracted_root, extract_dir = extracted

    mvn_custom = jdk_base / "mvn-custom"
    if mvn_custom.exists():
        shutil.rmtree(mvn_custom, ignore_errors=True)
    shutil.move(str(extracted_root), str(mvn_custom))
    shutil.rmtree(extract_dir, ignore_errors=True)

    mvn_bin_dir = mvn_custom / "bin"
    mvn_executable = mvn_bin_dir / ("mvn.cmd" if os_name == "windows" else "mvn")
    if os_name != "windows" and mvn_executable.exists():
        try:
            mvn_executable.chmod(0o755)
        except Exception:
            pass

    log(f"[OK] Maven installed at: {mvn_custom}")
    return mvn_custom, mvn_executable


class JavaVersionStep(ValidationStep):
    description = "Detecting Java version from pom.xml"

    def execute(self, ctx: ProjectContext) -> bool:
        java_version = parse_java_version_from_pom(ctx.pom_path)
        if not java_version:
            log("[ERROR] Unable to configure automatically: Java version not found in pom.xml.")
            return False
        ctx.java_version = java_version
        return True


class EnvironmentStep(ValidationStep):
    description = "Validating system and preparing directories"

    def execute(self, ctx: ProjectContext) -> bool:
        os_name, arch = detect_os_arch()
        log(f"[INFO] Detected OS/arch: {os_name}/{arch}")
        ctx.os_name, ctx.arch = os_name, arch
        if arch not in ("x86_64", "aarch64"):
            log("[ERROR] Architecture not supported. Only x64 and ARM64 (aarch64) are supported.")
            return False
        ensure_dirs()
        return True


class RuntimeProvisionStep(ValidationStep):
    description = "Provisioning JDK and Maven"

    def execute(self, ctx: ProjectContext) -> bool:
        if not ctx.java_version or not ctx.os_name or not ctx.arch:
            log("[ERROR] Invalid context for runtime provisioning.")
            return False

        existing_jdk = locate_existing_jdk(ctx.java_version)
        if existing_jdk:
            log(f"[OK] JDK already present: {existing_jdk}")

        existing_maven = locate_existing_maven(ctx.java_version, ctx.os_name)
        if existing_maven:
            log(f"[OK] Maven already present: {existing_maven[0]}")

        future_jdk: Optional[Future[Optional[Tuple[Path, JdkDist]]]] = None
        future_maven: Optional[Future[Optional[Tuple[Path, str]]]] = None
        with ThreadPoolExecutor(max_workers=2) as executor:
            if not existing_jdk:
                future_jdk = executor.submit(
                    download_jdk_package, ctx.java_version, ctx.os_name, ctx.arch
                )
            if not existing_maven:
                future_maven = executor.submit(download_maven_package, ctx.os_name)

        jdk_home: Optional[Path] = existing_jdk
        jdk_download: Optional[Tuple[Path, JdkDist]] = None
        if future_jdk is not None:
            jdk_download = future_jdk.result()
            if jdk_download is None:
                log("[ERROR] JDK download failed. Aborting for this project.")
                return False

        if jdk_home is None and jdk_download is not None:
            archive, dist = jdk_download
            jdk_home = install_jdk_from_archive(ctx.java_version, archive, dist)
            if not jdk_home:
                log("[WARN] Initial JDK installation failed; trying alternate distributors.")
                jdk_home = install_jdk_with_fallback(ctx.java_version, ctx.os_name, ctx.arch, {dist})
            if not jdk_home:
                log("[ERROR] JDK installation failed. Aborting for this project.")
                return False

        if jdk_home is None:
            log("[ERROR] Unable to prepare the required JDK.")
            return False

        if existing_maven:
            maven_home, maven_bin = existing_maven
        else:
            maven_download: Optional[Tuple[Path, str]] = None
            if future_maven is not None:
                maven_download = future_maven.result()
            if not maven_download:
                log("[ERROR] Maven download failed. Aborting for this project.")
                return False
            archive, _ = maven_download
            maven_result = install_maven_from_archive(ctx.java_version, ctx.os_name, archive)
            if not maven_result:
                log("[ERROR] Maven installation failed. Aborting for this project.")
                return False
            maven_home, maven_bin = maven_result

        ctx.jdk_home = jdk_home
        ctx.maven_home = maven_home
        ctx.maven_bin = maven_bin
        return True


class ConfigurationStep(ValidationStep):
    description = "Applying environment configuration"

    def execute(self, ctx: ProjectContext) -> bool:
        if not ctx.java_version or not ctx.jdk_home or not ctx.maven_bin:
            log("[ERROR] Incomplete context to configure environment.")
            return False

        try:
            merge_toolchains(ctx.java_version, ctx.jdk_home)
        except Exception as exc:
            log(f"[WARN] Failed to configure toolchains: {exc}")

        try:
            ensure_settings_xml()
        except Exception as exc:
            log(f"[WARN] Failed to ensure settings.xml: {exc}")

        try:
            update_vscode_settings(ctx.project_dir, ctx.jdk_home, ctx.maven_bin)
        except Exception as exc:
            log(f"[WARN] Failed to update VS Code settings: {exc}")
            return False

        return True


def process_project(project_dir: Path) -> None:
    pom = project_dir / "pom.xml"
    if not pom.exists():
        log(f"[SKIP] No pom.xml in {project_dir}; skipping.")
        return

    log(f"[INFO] Processing project: {project_dir}")
    ctx = ProjectContext(project_dir=project_dir, pom_path=pom)
    chain = ValidationChain(
        [
            JavaVersionStep(),
            EnvironmentStep(),
            RuntimeProvisionStep(),
            ConfigurationStep(),
        ]
    )

    if chain.run(ctx):
        log(f"[OK] Project configured: {project_dir}")


def cleanup_temp() -> None:
    leftovers: List[Path] = []
    try:
        if TEMP_DIR.exists():
            for child in TEMP_DIR.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except Exception:
                    leftovers.append(child)
        if leftovers:
            log("[WARN] Temporary files not removed:")
            for path in leftovers:
                log(f"  - {path}")
        else:
            log("[OK] Temporary folder cleaned.")
    except Exception as exc:
        log(f"[WARN] Failed to clean temporary files: {exc}")


def main() -> None:
    root = Path.cwd()
    log(f"[START] Jaenvtix Setup in workspace: {root}")

    projects = find_projects_with_pom(root)
    if not projects:
        log("[INFO] No pom.xml found in workspace. Nothing to do.")
        return

    for project in projects:
        try:
            process_project(project)
        except Exception as exc:
            log(f"[ERROR] Failed to process project {project}: {exc}")

    cleanup_temp()
    log("[DONE] Completed.")


if __name__ == "__main__":
    main()
