"""MHLW(医療情報ネット)診療科コード -> Crestix対象9科 のmapping定義。

Git管理対象の「ソース」。CSVやDBを一切読まない純粋なモジュールなので、
MHLW元CSVやsidecar DBが手元になくても import してテストできる
(tests/test_mhlw_crestix_mapping.py 参照)。

Crestix canonical department vocabulary is taken from the EXISTING production config
config/treatment_taxonomy.yml (crestix_sales_categories keys), NOT invented fresh, to stay
consistent with the running system:
  消化器内科, 眼科, 糖尿病内科, 泌尿器科, 循環器内科, 皮膚科, 歯科, 美容整形外科, 産婦人科

No substring ("contains") matching is used anywhere. Every decision below is an explicit
lookup keyed by the MHLW controlled department CODE (and cross-checked by literal name),
decided by a human-reviewable table, not inferred by string containment.
"""

CRESTIX_TARGETS = ["消化器内科", "眼科", "糖尿病内科", "泌尿器科", "循環器内科", "皮膚科", "歯科", "美容整形外科", "産婦人科"]

# code -> (status, crestix_department, reason)
DECISIONS = {
    # --- EXACT: literal name identical to a Crestix canonical target ---
    "01006": ("EXACT", "糖尿病内科", "MHLW統制語彙の名称がCrestix対象科目名と完全一致"),
    "01017": ("EXACT", "循環器内科", "MHLW統制語彙の名称がCrestix対象科目名と完全一致"),
    "01020": ("EXACT", "消化器内科", "MHLW統制語彙の名称がCrestix対象科目名と完全一致"),
    "04001": ("EXACT", "産婦人科", "MHLW統制語彙の名称がCrestix対象科目名と完全一致"),
    "05001": ("EXACT", "眼科", "MHLW統制語彙の名称がCrestix対象科目名と完全一致"),
    "06001": ("EXACT", "皮膚科", "MHLW統制語彙の名称がCrestix対象科目名と完全一致"),
    "06004": ("EXACT", "泌尿器科", "MHLW統制語彙の名称がCrestix対象科目名と完全一致"),
    "08001": ("EXACT", "歯科", "MHLW統制語彙の名称がCrestix対象科目名と完全一致"),
    # --- ALIAS: unambiguous synonym (MHLW's standard term for the same clinical category) ---
    "02037": ("ALIAS", "美容整形外科", "MHLW統制語彙の標準名は「美容外科」。Crestix側正式名称「美容整形外科」と同一カテゴリの別表記"),
    # --- EXCLUDE: explicit false-friend (name resembles a target but is a distinct specialty) ---
    "02035": ("EXCLUDE", "", "「整形外科」＝整形外科（骨・関節）。「美容整形外科」とは全く別の診療科。名称類似のみでの混同を禁止(個別確認対象として明示要求あり)"),
    "07003": ("EXCLUDE", "", "「心療内科」は心身医学（メンタルヘルス）領域。「内科」を含むが消化器内科等の対象科目ではない(個別確認対象として明示要求あり)"),
    "07005": ("EXCLUDE", "", "「老年心療内科」も心療内科と同じ心身医学領域。対象科目ではない"),
    # --- REVIEW: explicitly requested individual checks ---
    "01021": ("REVIEW", "消化器内科(候補)", "「胃腸内科」は消化器内科と近接領域だが正式名称が異なる。個別確認対象として明示要求あり"),
    "01011": ("REVIEW", "糖尿病内科(候補)", "「糖尿病・代謝内科」は複合科名。個別確認対象として明示要求あり"),
    "01010": ("REVIEW", "糖尿病内科(候補)", "「糖尿病・内分泌内科」は複合科名で糖尿病内科を含む"),
    "06002": ("REVIEW", "皮膚科or美容整形外科(候補)", "「美容皮膚科」は皮膚科・美容整形外科どちらの対象科目にも重なりうる。個別確認対象として明示要求あり"),
    "02036": ("REVIEW", "美容整形外科(候補)", "「形成外科」は保険診療の再建外科が中心で美容外科と重なる場合があるが同一ではない。個別確認対象として明示要求あり"),
    "04002": ("REVIEW", "産婦人科(候補)", "「産科」は産婦人科の産科領域のみのサブセット。個別確認対象として明示要求あり"),
    "04003": ("REVIEW", "産婦人科(候補)", "「婦人科」は産婦人科の婦人科領域のみのサブセット。個別確認対象として明示要求あり"),
    # --- REVIEW: adjacent/combined department names found in the controlled vocabulary (not explicitly
    #     requested by name, but structurally the same class of ambiguity; not auto-mapped) ---
    "01007": ("REVIEW", "糖尿病内科(候補)", "「代謝内科」は代謝疾患全般で糖尿病を含み得るが同義ではない"),
    "01009": ("REVIEW", "糖尿病内科(候補)", "「脂質代謝内科」は生活習慣病領域で糖尿病内科と近接"),
    "01012": ("REVIEW", "糖尿病内科(候補)", "「代謝・内分泌内科」は複合科名"),
    "01018": ("REVIEW", "循環器内科(候補)", "「心臓内科」は循環器内科と近接領域だが正式名称が異なる"),
    "01019": ("REVIEW", "循環器内科(候補)", "「心臓血管内科」は循環器内科と近接領域"),
    "01024": ("REVIEW", "消化器内科(候補)", "「肝臓内科」は消化器領域のサブスペシャリティ"),
    "01030": ("REVIEW", "消化器内科(候補)", "「内視鏡内科」は消化器診療の主要手技領域"),
    "02008": ("REVIEW", "循環器内科(候補)", "「血管外科」は循環器隣接領域だが外科"),
    "02009": ("REVIEW", "循環器内科(候補)", "「循環器外科」は循環器内科と近接するが外科"),
    "02010": ("REVIEW", "循環器内科(候補)", "「心臓外科」は循環器隣接領域だが外科"),
    "02011": ("REVIEW", "循環器内科(候補)", "「心臓血管外科」は循環器隣接領域だが外科"),
    "02012": ("REVIEW", "消化器内科(候補)", "「消化器外科」は消化器内科と近接するが外科"),
    "02014": ("REVIEW", "消化器内科(候補)", "「消化器・移植外科」は複合科名で外科"),
    "02015": ("REVIEW", "消化器内科(候補)", "「胃腸外科」は消化器隣接領域だが外科"),
    "02018": ("REVIEW", "消化器内科(候補)", "「肝臓外科」は消化器隣接領域だが外科"),
    "02021": ("REVIEW", "消化器内科(候補)", "「肝臓・胆のう・膵臓外科」は消化器隣接の複合科名"),
    "02032": ("REVIEW", "消化器内科(候補)", "「内視鏡外科」は消化器診療と重なるが外科"),
    "02034": ("REVIEW", "消化器内科(候補)", "「移植・内視鏡外科」は複合科名"),
    "03002": ("REVIEW", "眼科(候補)", "「小児眼科」は眼科のサブスペシャリティ(小児)"),
    "03004": ("REVIEW", "皮膚科(候補)", "「小児皮膚科」は皮膚科のサブスペシャリティ(小児)"),
    "03007": ("REVIEW", "泌尿器科(候補)", "「小児泌尿器科」は泌尿器科のサブスペシャリティ(小児)"),
    "04004": ("REVIEW", "産婦人科(候補)", "「産婦人科（生殖医療）」は産婦人科のサブスペシャリティ(生殖医療)"),
    "06003": ("REVIEW", "皮膚科or泌尿器科(候補)", "「皮膚泌尿器科」は皮膚科と泌尿器科の複合科名"),
    "06005": ("REVIEW", "泌尿器科(候補)", "「男性泌尿器科」は泌尿器科のサブスペシャリティ"),
    "06006": ("REVIEW", "泌尿器科(候補)", "「神経泌尿器科」は泌尿器科のサブスペシャリティ"),
    "06007": ("REVIEW", "泌尿器科(候補)", "「腎臓・泌尿器科」は泌尿器科を含む複合科名"),
    "08002": ("REVIEW", "歯科(候補)", "「矯正歯科」は歯科のサブスペシャリティ(矯正)。単科の場合は一般歯科対象外の可能性あり"),
    "08003": ("REVIEW", "歯科(候補)", "「歯科口腔外科」は歯科のサブスペシャリティ(口腔外科)"),
    "08004": ("REVIEW", "歯科(候補)", "「小児歯科」は歯科のサブスペシャリティ(小児)"),
    "08005": ("REVIEW", "歯科(候補)", "「小児矯正歯科」は歯科のサブスペシャリティ(小児矯正)"),
}

FREETEXT_MIN_SUFFIX = 900  # codes like 01991, 05992, 06992... = per-line free-text/other bucket


def classify(code, name):
    """department_code(例:"01020") -> (status, crestix_department, reason)。

    substring("内科"を含むから対象、等)判定は一切行わない。DECISIONSの明示lookupか、
    自由記載バケット(末尾3桁>=900)判定のみ。どちらにも該当しなければUNMAPPED。
    """
    if code in DECISIONS:
        return DECISIONS[code]
    suffix = int(code[2:]) if code[2:].isdigit() else 0
    if suffix >= FREETEXT_MIN_SUFFIX:
        return "EXCLUDE", "", "自由記載(非統制語彙)区分のコード。MHLW統制語彙の正式診療科名ではないため対象外"
    return "UNMAPPED", "", "Crestix対象9診療科のいずれにも該当しない統制語彙上の診療科"
