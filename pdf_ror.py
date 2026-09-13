"""Decode and validate the portal's PDF settlement RoRs without OCR or network access."""

from __future__ import annotations

import re
import unicodedata
from io import BytesIO

import pdfplumber
from fontTools.ttLib import TTFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

EXTRACTOR_VERSION = 2
REPH = "\ue000"
CONSONANT = r"[\u0b15-\u0b39\u0b5c\u0b5d\u0b5f\u0b71]"
CLUSTER = CONSONANT + r"\u0b3c?(?:\u0b4d" + CONSONANT + r"\u0b3c?)*"
MARKS = r"[\u0b01-\u0b03\u0b3c\u0b3e-\u0b44\u0b47-\u0b4c\u0b56\u0b57]*"


class PDFExtractionError(ValueError):
    """A PDF cannot be decoded or does not match the requested record."""


class PDFProvenanceError(PDFExtractionError):
    """A complete PDF belongs to a different request identity."""


class PDFSourceIncompleteError(PDFExtractionError):
    """The portal returned a record header without its owner table."""


def logical_text(text: str) -> str:
    """Undo Kalinga's visual-order reph and split/pre-base vowel placement."""
    text = text.replace("\u200b", "")
    text = re.sub("(" + CLUSTER + MARKS + ")" + REPH, r"ର୍\1", text)
    text = re.sub("େ(" + CLUSTER + ")", r"\1େ", text)
    text = re.sub("(" + CONSONANT + ")([\u0b3e-\u0b44])଼", r"\1଼\2", text)
    text = re.sub(r"([\u0b01-\u0b03]+)([\u0b3e-\u0b44\u0b47-\u0b4c])", r"\2\1", text)
    text = unicodedata.normalize("NFC", text)
    if REPH in text or "(cid:" in text or "\ufffd" in text:
        raise PDFExtractionError("unresolved PDF glyph or reordering")
    return " ".join(text.split())


def identity(text: str) -> str:
    """Canonicalize identifier spacing, numeral script and combining-mark order."""
    text = logical_text(text).replace("୍ଵ", "୍ବ").replace("୍ୱ", "୍ବ")
    text = re.sub(
        "(" + CLUSTER + ")", lambda m: m[0].replace("଼", "") + "଼" * m[0].count("଼"), text
    )
    return "".join(
        str(unicodedata.digit(char)) if char.isdecimal() else char
        for char in text
        if not char.isspace() and char not in "\u200b\u200c\u200d"
    )


def nominal_glyphs(font: TTFont) -> dict[int, str]:
    """Recover Kalinga 6's nominal glyphs, including subset-omitted GSUB inputs.

    The font's Odia nominal block occupies glyphs 231..311. Its repertoire
    predates U+0B44, U+0B55, U+0B62 and U+0B63. Every surviving cmap entry is
    checked against this profile before omitted entries can be used.
    """
    if font["head"].fontRevision != 6.0 or len(font.getGlyphOrder()) not in (634, 646):
        raise PDFExtractionError("unsupported Kalinga font revision or glyph order")
    odia = "ଁଂଃଅଆଇଈଉଊଋଌଏଐଓଔକଖଗଘଙଚଛଜଝଞଟଠଡଢଣତଥଦଧନପଫବଭମଯରଲଳଵଶଷସହ" "଼ଽାିୀୁୂୃେୈୋୌ୍ୖୗଡ଼ଢ଼ୟୠୡ୦୧୨୩୪୫୬୭୮୯୰ୱ"
    result = dict(enumerate(odia, 231))
    result.update({point - 29: chr(point) for point in range(32, 127)})
    cmap = font.getBestCmap()
    if not cmap:
        raise PDFExtractionError("PDF font has no usable cmap anchors")
    anchors = 0
    for point, name in cmap.items():
        glyph = font.getGlyphID(name)
        value = chr(point)
        if glyph in result:
            if result[glyph] != value:
                raise PDFExtractionError("Kalinga cmap disagrees with nominal glyph profile")
            anchors += 1
        result[glyph] = value
    if anchors < 10:
        raise PDFExtractionError("insufficient Kalinga cmap anchors")
    return result


def glyph_mapping(font: TTFont, *, kalinga: bool = True) -> dict[int, str]:
    """Reverse the embedded font's substitutions into Unicode sequences."""
    result = (
        nominal_glyphs(font)
        if kalinga
        else {
            font.getGlyphID(name): chr(point) for point, name in (font.getBestCmap() or {}).items()
        }
    )
    if not result:
        raise PDFExtractionError("PDF font has no Unicode mapping")
    if "GSUB" not in font and not kalinga:
        return result
    rules = []
    rephs = set()
    gsub = font["GSUB"].table
    for feature in gsub.FeatureList.FeatureRecord:
        if kalinga and feature.FeatureTag == "rphf":
            rephs.update(feature.Feature.LookupListIndex)
    reph_glyphs = set()
    for index, lookup in enumerate(gsub.LookupList.Lookup):
        for table in lookup.SubTable:
            if lookup.LookupType == 1:
                rules.extend(
                    (font.getGlyphID(target), [font.getGlyphID(source)])
                    for source, target in table.mapping.items()
                )
            elif lookup.LookupType == 4:
                for source, ligatures in table.ligatures.items():
                    for ligature in ligatures:
                        target = font.getGlyphID(ligature.LigGlyph)
                        components = [font.getGlyphID(g) for g in [source] + ligature.Component]
                        rules.append((target, components))
                        if index in rephs:
                            reph_glyphs.add(target)
    single_targets = {target for target, components in rules if len(components) == 1}
    for _ in range(len(font.getGlyphOrder())):
        changed = False
        for target, components in rules:
            if len(components) > 1 and target in single_targets:
                continue
            if target not in result and all(glyph in result for glyph in components):
                result[target] = "".join(result[glyph] for glyph in components)
                changed = True
        if not changed:
            break
    for glyph in reph_glyphs:
        if result.get(glyph) != "ର୍":
            raise PDFExtractionError("unexpected reph substitution")
        result[glyph] = REPH
    return result


def unicode_cmap(mapping: dict[int, str]) -> DecodedStreamObject:
    """Construct an in-memory PDF ToUnicode map; the original PDF stays unchanged."""
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Recovered def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    pairs = list(mapping.items())
    for start in range(0, len(pairs), 100):
        batch = pairs[start : start + 100]
        lines.append(f"{len(batch)} beginbfchar")
        lines.extend(f"<{key:04X}> <{value.encode('utf-16-be').hex()}>" for key, value in batch)
        lines.append("endbfchar")
    lines.extend(["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"])
    stream = DecodedStreamObject()
    stream.set_data("\n".join(lines).encode("ascii"))
    return stream


def restore_unicode(data: bytes) -> tuple[BytesIO, int]:
    """Validate the PDF and repair its missing character maps in memory."""
    if not data.startswith(b"%PDF-") or not data.rstrip().endswith(b"%%EOF"):
        raise PDFExtractionError("invalid or truncated PDF")
    reader = PdfReader(BytesIO(data), strict=True)
    if reader.is_encrypted or not reader.pages:
        raise PDFExtractionError("encrypted or empty PDF")
    mapped = set()
    for page in reader.pages:
        for reference in page["/Resources"].get("/Font", {}).values():
            font_dict = reference.get_object()
            if id(font_dict) in mapped or "/ToUnicode" in font_dict:
                continue
            mapped.add(id(font_dict))
            base = str(font_dict.get("/BaseFont", ""))
            if font_dict.get("/Subtype") != "/Type0":
                continue
            descendant = font_dict["/DescendantFonts"][0].get_object()
            if descendant.get("/CIDToGIDMap", "/Identity") != "/Identity":
                raise PDFExtractionError("unsupported nonidentity PDF glyph mapping")
            binary = descendant["/FontDescriptor"]["/FontFile2"].get_data()
            with TTFont(BytesIO(binary)) as font:
                font_dict[NameObject("/ToUnicode")] = unicode_cmap(
                    glyph_mapping(font, kalinga="Kalinga" in base)
                )
    writer = PdfWriter()
    writer.append(reader)
    restored = BytesIO()
    writer.write(restored)
    restored.seek(0)
    return restored, len(reader.pages)


def extract_tables(tables: list[list[list[str | None]]], expected: dict) -> dict:
    """Validate printed identity and retain all owner blocks across every page."""
    cells = []
    provenance = {}
    body = False
    total = False
    repeated = 0
    for table in tables:
        for raw_row in table:
            row = [logical_text(value) if value is not None else None for value in raw_row]
            first = row[0] or ""
            if first.startswith("ରାଜସ୍") and "ଗ୍ରାମ" in first:
                village = row[1]
                if not village or identity(village) not in {
                    identity(expected["village_name"]),
                    identity(expected.get("pdf_village_alias", expected["village_name"])),
                }:
                    raise PDFProvenanceError("PDF village does not match requested village")
                provenance["village_name"] = village
                body = False
            for index, value in enumerate(row):
                if value == "ଖାତା ନଂ":
                    khatiyan = next((v for v in row[index + 1 :] if v), "")
                    if identity(khatiyan) != identity(expected["khatiyan"]):
                        raise PDFProvenanceError("PDF khatiyan does not match requested khatiyan")
                    provenance["khatiyan"] = khatiyan
            if first.startswith("ଜମି ମାଲିକ"):
                body = True
                continue
            if not body or not first or identity(first) == "1":
                continue
            if first.lower() == "total":
                total = True
                body = False
                continue
            if first in cells:
                repeated += 1
            else:
                cells.append(first)
    if not provenance.get("village_name") or not provenance.get("khatiyan"):
        raise PDFExtractionError("PDF lacks required record identity")
    if not cells and not body:
        raise PDFSourceIncompleteError("PDF has record identity but no owner table")
    if not total or not cells:
        raise PDFExtractionError("PDF owner table is incomplete or empty")
    return {"cells": cells, "provenance": provenance, "repeated_owner_blocks": repeated}


def extract_pdf(data: bytes, expected: dict) -> dict:
    """Decode every page of a PDF RoR, failing closed on unrecognized content."""
    restored, pages = restore_unicode(data)
    with pdfplumber.open(restored) as document:
        tables = []
        for page in document.pages:
            page = page.dedupe_chars()
            text = page.extract_text() or ""
            if "(cid:0)" in text:
                raise PDFSourceIncompleteError("PDF source contains an undefined glyph")
            if "(cid:" in text or "\ufffd" in text:
                raise PDFExtractionError("PDF contains unmapped glyphs")
            page_tables = page.extract_tables()
            if not page_tables:
                certificate = (
                    "Certified that the ROR has been published on dated ____________ "
                    "under Section 11(1) of the Odisha Special Survey & Settlement Act 2012 "
                    "read with Rule 14 and 15 Of Odisha Special Survey & Settlement Act 2012."
                )
                if re.sub(r"[^a-z0-9]", "", text.lower()) == re.sub(
                    r"[^a-z0-9]", "", certificate.lower()
                ):
                    continue
                raise PDFExtractionError("PDF page has no recognized table")
            tables.extend(page_tables)
        result = extract_tables(tables, expected)
    return {**result, "page_count": pages, "extractor_version": EXTRACTOR_VERSION}
