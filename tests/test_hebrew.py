from tivtaam.util.hebrew import normalize, tokens


def test_normalize_strips_niqqud():
    assert normalize("גְּבִינָה") == "גבינה"


def test_normalize_strips_bidi_marks():
    assert normalize("‏גבינה‏") == "גבינה"


def test_normalize_collapses_whitespace():
    assert normalize("גבינה   בולגרית\t  5%") == "גבינה בולגרית 5%"


def test_normalize_lowercases_latin():
    assert normalize("Gad GBINA 5%") == "gad gbina 5%"


def test_normalize_handles_geresh():
    assert normalize("ק״ג") == 'ק"ג'
    assert normalize("קוטג׳") == "קוטג'"


def test_normalize_empty():
    assert normalize("") == ""


def test_tokens_split_on_whitespace_and_punctuation():
    t = tokens("גבינה בולגרית 5% (גד)")
    assert "גבינה" in t and "בולגרית" in t and "5%" in t
