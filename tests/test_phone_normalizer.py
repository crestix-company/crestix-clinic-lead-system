import pytest
from src.normalizer.phone import normalize_phone
from src.normalizer.clinic_name import normalize_clinic_name, normalize_person, surname
from src.normalizer.address import normalize_address


@pytest.mark.parametrize("raw,expected", [
    ("03-1234-5678","0312345678"), ("(03)1234-5678","0312345678"),
    ("０３－１２３４－５６７８","0312345678"), ("", ""), (None,""),
    ("03-1234-5678 内線99","0312345678"), ("312345678.0","312345678"),
])
def test_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_name_and_address_variants():
    assert normalize_person("髙原　一真") == normalize_person("高原 一真")
    assert normalize_clinic_name("医療法人社団 試験会 青空クリニック") == "青空クリニック"
    assert normalize_clinic_name("青空医院", True) == normalize_clinic_name("青空クリニック", True)
    assert normalize_address("〒100-0000 東京都千代田区架空町三丁目2番1号") == normalize_address("東京都千代田区架空町3-2-1")
    assert surname("山田太郎") == ""  # 姓・名の境界を推測しない
    assert surname("山田 太郎") == "山田"
    assert normalize_clinic_name("ハート医院") != normalize_clinic_name("ハト医院")
