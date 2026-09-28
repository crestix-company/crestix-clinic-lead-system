"""HPランク改善 Phase2.1: C/D専用D detector candidate（未採用・検証専用）。

Phase2のcache済みテキスト特徴（copyright年等）はD recallが学習240件で
19.4%まで見えたが、検証60件では0/9で汎化しなかった。Phase2.1では
学習240件のHP URLから実HTMLを再取得し（scratch領域のみ、Production DB無変更）、
画像・電話CTA・HTMLサイズ等の「cacheに無かった特徴」を検証した。

学習240件のC/D比較（実HTML、n=165、内139件取得成功):
  clinic_photo_many (img>=10枚)     : C 85% vs D 61% (diff +24%) - 最強
  doctor_photo_candidate (院長/医師の img alt): C 39% vs D 16% (diff +23%)
  phone_cta (tel:リンク)            : C 81% vs D 65% (diff +17%)
  html_byte_size                    : C平均165,570 vs D平均77,007（Dはほぼ半分）

この4指標のうち3つ以上が「弱い」側に該当したらDと判定する多数決ルールを
学習240件で確定し、検証60件（HTML再取得、今回1回だけ適用）へ適用した結果:

  train(n=165): D_recall 0%->48.4%, D_precision 42.9%, accuracy 78.2%
  val  (n=44) : D_recall 0%->22.2%, D_precision 20.0%, accuracy 65.9%

検証データでも0%から離れて汎化した（Phase2のcopyright特徴は0/9で汎化せず、
今回は2/9を検出）。ただし絶対値としてはまだ弱く、D=40件という
サンプル数の制約が大きい。本番反映は推奨しない（要追加人間ラベル）。
"""


def d_detector_features(html_features):
    """4つの弱いシグナルのうち何個が「Dらしい」側かを数える。"""
    return {
        "not_many_photos": not html_features["clinic_photo_many"],
        "no_doctor_photo": not html_features["doctor_photo_candidate"],
        "small_html": html_features["html_byte_size"] < 60000,
        "no_phone_cta": not html_features["phone_cta"],
    }


def d_detector_vote_count(html_features):
    return sum(d_detector_features(html_features).values())


def is_d_candidate(html_features, min_votes=3):
    return d_detector_vote_count(html_features) >= min_votes
