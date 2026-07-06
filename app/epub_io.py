from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET


XHTML_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}


@dataclass(frozen=True)
class EpubChapter:
    index: int
    href: str
    abs_path: Path
    title: str | None = None


@dataclass(frozen=True)
class EpubInfo:
    opf_path: Path
    opf_dir: Path
    chapters: list[EpubChapter]


def unpack_epub(source_path: Path, work_dir: Path) -> None:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(source_path, "r") as zf:
        names = set(zf.namelist())
        if "mimetype" not in names:
            raise ValueError("EPUB is missing mimetype")
        if "META-INF/container.xml" not in names:
            raise ValueError("EPUB is missing META-INF/container.xml")
        zf.extractall(work_dir)


def read_epub_info(work_dir: Path) -> EpubInfo:
    container_path = work_dir / "META-INF" / "container.xml"
    container_root = ET.parse(container_path).getroot()
    ns = {"c": "urn:oasis:names:tc:opendocument:xmlns:container"}
    rootfile = container_root.find(".//c:rootfile", ns)
    if rootfile is None:
        rootfile = container_root.find(".//rootfile")
    if rootfile is None or not rootfile.attrib.get("full-path"):
        raise ValueError("EPUB container.xml does not declare a rootfile")

    opf_rel = PurePosixPath(rootfile.attrib["full-path"])
    opf_path = work_dir / Path(*opf_rel.parts)
    opf_dir = opf_path.parent

    opf_root = ET.parse(opf_path).getroot()
    opf_ns = _namespace(opf_root.tag)
    manifest: dict[str, tuple[str, str]] = {}
    for item in opf_root.findall(f".//{_q(opf_ns, 'manifest')}/{_q(opf_ns, 'item')}"):
        item_id = item.attrib.get("id")
        href = item.attrib.get("href")
        media_type = item.attrib.get("media-type")
        if item_id and href and media_type:
            manifest[item_id] = (href, media_type)

    chapters: list[EpubChapter] = []
    for itemref in opf_root.findall(f".//{_q(opf_ns, 'spine')}/{_q(opf_ns, 'itemref')}"):
        idref = itemref.attrib.get("idref")
        if not idref or idref not in manifest:
            continue
        href, media_type = manifest[idref]
        if media_type not in XHTML_MEDIA_TYPES:
            continue
        chapter_abs = (opf_dir / Path(*PurePosixPath(href).parts)).resolve()
        chapters.append(EpubChapter(index=len(chapters), href=href, abs_path=chapter_abs))

    return EpubInfo(opf_path=opf_path, opf_dir=opf_dir, chapters=chapters)


def _namespace(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", 1)[0]
    return ""


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}" if namespace else tag
