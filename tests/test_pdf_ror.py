"""PDF decoding, identity and full-page extraction regression tests."""

from types import SimpleNamespace

import pytest

from pdf_ror import (
    PDFExtractionError,
    PDFProvenanceError,
    extract_tables,
    glyph_mapping,
    logical_text,
    nominal_glyphs,
    restore_unicode,
)


@pytest.mark.parametrize(
    ("visual", "logical"),
    [
        ("େଦବଗଡ", "ଦେବଗଡ"),
        ("େସାନପୁର", "ସୋନପୁର"),
        ("ସୁବଣ୍ଣ\ue000ପୁର", "ସୁବର୍ଣ୍ଣପୁର"),
        ("ଡୁ଼ଙ୍ଗୁରି", "ଡ଼ୁଙ୍ଗୁରି"),
    ],
)
def test_visual_order_is_recovered(visual, logical):
    assert logical_text(visual) == logical


class Font:
    def __init__(self):
        self.revision = 6.0
        self.cmap = {point: str(point - 29) for point in range(32, 50)}
        self.gsub = SimpleNamespace(
            FeatureList=SimpleNamespace(
                FeatureRecord=[
                    SimpleNamespace(FeatureTag="rphf", Feature=SimpleNamespace(LookupListIndex=[1]))
                ]
            ),
            LookupList=SimpleNamespace(
                Lookup=[
                    SimpleNamespace(
                        LookupType=4,
                        SubTable=[
                            SimpleNamespace(
                                ligatures={
                                    "250": [
                                        SimpleNamespace(LigGlyph="331", Component=["292", "246"])
                                    ]
                                }
                            )
                        ],
                    ),
                    SimpleNamespace(
                        LookupType=4,
                        SubTable=[
                            SimpleNamespace(
                                ligatures={
                                    "272": [SimpleNamespace(LigGlyph="445", Component=["292"])]
                                }
                            )
                        ],
                    ),
                ]
            ),
        )

    def __contains__(self, name):
        return name in {"head", "GSUB"}

    def __getitem__(self, name):
        if name == "head":
            return SimpleNamespace(fontRevision=self.revision)
        return SimpleNamespace(table=self.gsub)

    def getGlyphOrder(self):
        return [str(index) for index in range(634)]

    def getBestCmap(self):
        return self.cmap

    def getGlyphID(self, name):
        return int(name)


def test_subset_omitted_base_is_recovered_before_ligature():
    mapping = glyph_mapping(Font())
    assert mapping[250] == "ଙ"
    assert mapping[331] == "ଙ୍କ"
    assert logical_text("କ" + mapping[445]) == "ର୍କ"


def test_font_profile_must_match_surviving_cmap():
    font = Font()
    font.cmap[0xB15] = "250"
    with pytest.raises(PDFExtractionError, match="disagrees"):
        nominal_glyphs(font)
    font = Font()
    font.revision = 7.0
    with pytest.raises(PDFExtractionError, match="unsupported"):
        nominal_glyphs(font)


def tables():
    return [
        [
            ["ରାଜସ୍ଵ ଗ୍ରାମ -", "ଗ୍ରାମ"],
            ["ଖାତା ନଂ", "20"],
            ["ଜମି ମାଲିକଙ୍କ ନାମ"],
            ["୧"],
            ["A ପି:B ଜା: C"],
        ],
        [["A ପି:B ଜା: C"], ["D ପି:E ଜା: F"], ["Total"]],
    ]


def test_all_pages_are_extracted_without_repeated_owner_blocks():
    result = extract_tables(tables(), {"village_name": "ଗ୍ରାମ", "khatiyan": "20"})
    assert result["cells"] == ["A ପି:B ଜା: C", "D ପି:E ଜା: F"]
    assert result["repeated_owner_blocks"] == 1


@pytest.mark.parametrize(
    "expected",
    [
        {"village_name": "ଅନ୍ୟ", "khatiyan": "20"},
        {"village_name": "ଗ୍ରାମ", "khatiyan": "21"},
    ],
)
def test_wrong_record_is_rejected(expected):
    with pytest.raises(PDFProvenanceError):
        extract_tables(tables(), expected)


def test_late_page_unknown_glyph_and_missing_tail_are_rejected():
    rows = tables()
    rows[1][1][0] = "(cid:999)"
    with pytest.raises(PDFExtractionError, match="unresolved"):
        extract_tables(rows, {"village_name": "ଗ୍ରାମ", "khatiyan": "20"})
    with pytest.raises(PDFExtractionError, match="incomplete"):
        extract_tables(tables()[:1], {"village_name": "ଗ୍ରାମ", "khatiyan": "20"})
    with pytest.raises(PDFExtractionError, match="truncated"):
        restore_unicode(b"%PDF-1.3\nmissing trailer")


def test_approved_village_alias_does_not_relax_other_identity_checks():
    expected = {"village_name": "listed", "pdf_village_alias": "ଗ୍ରାମ", "khatiyan": "20"}
    assert extract_tables(tables(), expected)["cells"]
    expected["khatiyan"] = "21"
    with pytest.raises(PDFProvenanceError):
        extract_tables(tables(), expected)


def test_generic_embedded_font_uses_its_own_cmap():
    assert glyph_mapping(Font(), kalinga=False)[3] == " "
