from pathlib import Path
from datetime import datetime
import base64
import csv
import json
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import py_compile

ROOT = Path.cwd()
DB = ROOT / "data" / "clinics.sqlite3"
PROFILE = ROOT / "src" / "enrichment" / "profiles.py"

required = [DB, PROFILE, ROOT / "src/scoring/research_scoring.py"]
missing = [str(p) for p in required if not p.exists()]
if missing:
    print("エラー：clinic-list-filter-complete フォルダで実行してください。")
    for p in missing:
        print(" -", p)
    raise SystemExit(1)

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = ROOT / ("code_backup_research_v254_" + stamp)
backup.mkdir(parents=True, exist_ok=False)
shutil.copy2(PROFILE, backup / "profiles.py")

try:
    PROFILE.write_bytes(base64.b64decode('ZnJvbSBjb3B5IGltcG9ydCBkZWVwY29weQppbXBvcnQgcmUKaW1wb3J0IHVuaWNvZGVkYXRhCmZyb20gYnM0IGltcG9ydCBCZWF1dGlmdWxTb3VwCmZyb20gc3JjLnNjb3JpbmcuYWdlX2VzdGltYXRvciBpbXBvcnQgQWdlRXN0aW1hdG9yCmZyb20gc3JjLm5vcm1hbGl6ZXIuY2xpbmljX25hbWUgaW1wb3J0IG5vcm1hbGl6ZV9wZXJzb24KZnJvbSBzcmMudXRpbHMuZGF0ZV91dGlscyBpbXBvcnQgcGFyc2VfeWVhciwgcGFyc2VfZGF0ZSwgdG9kYXlfamFwYW4KCllFQVJfUEFUVEVSTiA9IHIiKD86MTlcZHsyfXwyMFxkezJ9fCg/OuW5s+aIkHzmmK3lkox85Luk5ZKMKVxzKig/OuWFg3xcZHsxLDJ9KSnlubQ/IgoKCmRlZiBfY2xlYW4odGV4dCk6CiAgICByZXR1cm4gdW5pY29kZWRhdGEubm9ybWFsaXplKCJORktDIixzdHIodGV4dCBvciAiIikpCgoKUk9MRV9OQU1FX1BBVFRFUk4gPSByZS5jb21waWxlKAogICAgIyB2MjUuMjog5qyh44Gu5Lq654mp6KaL5Ye644GX44Gv44CM5aeTIOWQjeOAjeOBoOOBkeOBp+OBquOBj+OAjOaWsOiwt+W8mOWun+OAjeOBruOCiOOBhuOBqgogICAgIyDnqbrnmb3jgarjgZfmsI/lkI3jgoLlooPnlYzjgajjgZfjgabmibHjgYbjgILljLvluKvlhY3oqLEv5Zu95a626Kmm6aiT44Gq44Gp44Gu5LiA6Iis6Kqe44Gv6Zmk5aSW44GZ44KL44CCCiAgICByIig/OumZoumVt3znkIbkuovplbd85Ymv6Zmi6ZW3fOWQjeiqiemZoumVt3znibnliKXpoafllY985ZCN6KqJ6aGn5ZWPfOmhp+WVj3znrqHnkIbogIV85ouF5b2T5Yy7KSIKICAgIHIiXHMqW++8mjpdP1xzKlvkuIAt6b6v44CF76iR6auZ76ec44O244O1XXsyLDEyfSIKICAgIHIifCg/Ol58W1xz44CAXSnljLvluKtccypb77yaOl0/XHMqKD8h5YWN6KixfOWbveWutuippumok3zntLnku4t85rOVfOS8mnzjgajjgZfjgaZ844GuKSIKICAgIHIiW+S4gC3pvq/jgIXvqJHpq5nvp5zjg7bjg7VdezIsMTJ9IgopCgoKZGVmIF9tYW5hZ2VyX3BhdHRlcm4obWFuYWdlcl9uYW1lKToKICAgIGNsZWFuZWQ9X2NsZWFuKG1hbmFnZXJfbmFtZSkuc3RyaXAoKQogICAgcGFydHM9W3JlLmVzY2FwZSh4KSBmb3IgeCBpbiByZS5zcGxpdChyIltcc+OAgF0rIixjbGVhbmVkKSBpZiB4XQogICAgaWYgbGVuKHBhcnRzKT49MjoKICAgICAgICByZXR1cm4gcmUuY29tcGlsZShyIlxzKiIuam9pbihwYXJ0cykpCiAgICBjb21wYWN0PXJlLmVzY2FwZShjbGVhbmVkLnJlcGxhY2UoIiAiLCIiKS5yZXBsYWNlKCLjgIAiLCIiKSkKICAgIHJldHVybiByZS5jb21waWxlKGNvbXBhY3QpCgoKZGVmIF90cmltX3RvX21hbmFnZXJfYmxvY2sodGV4dCwgbWFuYWdlcl9uYW1lLCBtYXhfbGVuPTE4MDApOgogICAgIiIi6KSH5pWw5Yy75bir44Oa44O844K444GL44KJ54++5Zyo44Gu566h55CG6ICF5pys5Lq644Gu57WM5q2044OW44Ot44OD44Kv44Gg44GR44KS5YiH44KK5Ye644GZ44CCIiIiCiAgICB0ZXh0PV9jbGVhbih0ZXh0KQogICAgcGF0PV9tYW5hZ2VyX3BhdHRlcm4obWFuYWdlcl9uYW1lKQogICAgbT1wYXQuc2VhcmNoKHRleHQpCiAgICBpZiBub3QgbToKICAgICAgICByZXR1cm4gIiIKCiAgICAjIHYyNS4yOiDmnKzkurrlkI3jgojjgorliY3jga7liKXljLvluKvjga7ntYzmrbTlubTjgpLmt7fjgZzjgarjgYTjgIIKICAgIHN0YXJ0PW1heCgwLG0uc3RhcnQoKS00MCkKICAgIGVuZD1taW4obGVuKHRleHQpLG0uZW5kKCkrbWF4X2xlbikKCiAgICAjIOasoeOBruW9ueiBtyArIOWIpeS6uuWQje+8iOepuueZveOBquOBl+awj+WQjeOCguWQq+OCgO+8ieOBjOWHuuOBn+OCieOAgeOBneOBk+OBp+WIh+OCi+OAggogICAgZm9yIG54dCBpbiBST0xFX05BTUVfUEFUVEVSTi5maW5kaXRlcih0ZXh0LG0uZW5kKCkpOgogICAgICAgIGJsb2NrPW54dC5ncm91cCgwKQogICAgICAgIGlmIG5vdCBwYXQuc2VhcmNoKGJsb2NrKToKICAgICAgICAgICAgZW5kPW1pbihlbmQsbnh0LnN0YXJ0KCkpCiAgICAgICAgICAgIGJyZWFrCgogICAgcmV0dXJuIHRleHRbc3RhcnQ6ZW5kXS5zdHJpcCgpCgoKCmRlZiBfbWFuYWdlcl9oZWFkaW5nX2NodW5rcyhzb3VwLCBtYW5hZ2VyX25hbWUsIG1heF9sZW49MTgwMCk6CiAgICAiIiLpmaLplbflkI3jgYzopovlh7rjgZcoaDEtaDYp44Gr44GC44KL5aC05ZCI44CB44Gd44Gu6KaL5Ye644GX44GL44KJ5qyh44Gu5Yil5Lq654mp6KaL5Ye644GX44G+44Gn44Gg44GR44KS5L2/44GG44CCCgogICAgdjI1LjI6CiAgICDljYrolLXploDog4Pohbjjgq/jg6rjg4vjg4Pjgq/jga7jgojjgYbjgasKICAgICAgaDMg6Zmi6ZW3IOaOm+iwt+WSjOS/igogICAgICAxOTgy5bm0IC4uLiDljLvlrabpg6jljZLmpa0KICAgICAgaDMg54m55Yil6aGn5ZWPIOaWsOiwt+W8mOWunwogICAgICAxOTYw5bm0IC4uLiAvIDE5NjPlubQgLi4uCiAgICDjgajntprjgY/jg5rjg7zjgrjjgafjgIHnibnliKXpoafllY/lgbTjga7lubTjgpLpmaLplbfntYzmrbTjgbjmt7fjgZzjgarjgYTjgIIKICAgICIiIgogICAgbmFtZT1ub3JtYWxpemVfcGVyc29uKG1hbmFnZXJfbmFtZSkKICAgIGlmIG5vdCBuYW1lOgogICAgICAgIHJldHVybiBbXQoKICAgIGNodW5rcz1bXQogICAgc2Vlbj1zZXQoKQogICAgaGVhZGluZ19uYW1lcz17ImgxIiwiaDIiLCJoMyIsImg0IiwiaDUiLCJoNiJ9CgogICAgZm9yIGhlYWRpbmcgaW4gc291cC5maW5kX2FsbChsaXN0KGhlYWRpbmdfbmFtZXMpKToKICAgICAgICBoZWFkaW5nX3RleHQ9X2NsZWFuKGhlYWRpbmcuZ2V0X3RleHQoIiAiLHN0cmlwPVRydWUpKQogICAgICAgIGlmIG5hbWUgbm90IGluIG5vcm1hbGl6ZV9wZXJzb24oaGVhZGluZ190ZXh0KToKICAgICAgICAgICAgY29udGludWUKCiAgICAgICAgcGFydHM9W2hlYWRpbmdfdGV4dF0KICAgICAgICBmb3IgZWxlbSBpbiBoZWFkaW5nLm5leHRfZWxlbWVudHM6CiAgICAgICAgICAgIGlmIGVsZW0gaXMgaGVhZGluZzoKICAgICAgICAgICAgICAgIGNvbnRpbnVlCgogICAgICAgICAgICBlbGVtX25hbWU9Z2V0YXR0cihlbGVtLCJuYW1lIixOb25lKQogICAgICAgICAgICBpZiBlbGVtX25hbWUgaW4gaGVhZGluZ19uYW1lczoKICAgICAgICAgICAgICAgIGNhbmRpZGF0ZT1fY2xlYW4oZWxlbS5nZXRfdGV4dCgiICIsc3RyaXA9VHJ1ZSkpCiAgICAgICAgICAgICAgICAjIOasoeOBruOAjOW9ueiBtyArIOWIpeS6uuWQjeOAjeimi+WHuuOBl+OBp+e1guS6huOAggogICAgICAgICAgICAgICAgaWYgUk9MRV9OQU1FX1BBVFRFUk4uc2VhcmNoKGNhbmRpZGF0ZSkgYW5kIG5hbWUgbm90IGluIG5vcm1hbGl6ZV9wZXJzb24oY2FuZGlkYXRlKToKICAgICAgICAgICAgICAgICAgICBicmVhawogICAgICAgICAgICAgICAgY29udGludWUKCiAgICAgICAgICAgICMgTmF2aWdhYmxlU3RyaW5nIOetieOBoOOBkeOCkuepjeOCgOOAguOCv+OCsOacrOS9k+OBr+WtkOWtq+ODhuOCreOCueODiOOBqOmHjeikh+OBmeOCi+OBn+OCgei/veWKoOOBl+OBquOBhOOAggogICAgICAgICAgICBpZiBlbGVtX25hbWUgaXMgTm9uZToKICAgICAgICAgICAgICAgIHBpZWNlPV9jbGVhbihzdHIoZWxlbSkpLnN0cmlwKCkKICAgICAgICAgICAgICAgIGlmIHBpZWNlOgogICAgICAgICAgICAgICAgICAgIHBhcnRzLmFwcGVuZChwaWVjZSkKCiAgICAgICAgICAgIGpvaW5lZD1yZS5zdWIociJccysiLCIgIiwiICIuam9pbihwYXJ0cykpLnN0cmlwKCkKICAgICAgICAgICAgaWYgbGVuKGpvaW5lZCk+PW1heF9sZW46CiAgICAgICAgICAgICAgICBicmVhawoKICAgICAgICBjaHVuaz1yZS5zdWIociJccysiLCIgIiwiICIuam9pbihwYXJ0cykpLnN0cmlwKClbOm1heF9sZW5dCiAgICAgICAgaWYgY2h1bmsgYW5kIHJlLnNlYXJjaChyIuWNkualrXzljLvluKvlhY3oqLF85Yy757GNfOWbveWutuippumokyIsY2h1bmspOgogICAgICAgICAgICBrZXk9Y2h1bmtbOjEyMDBdCiAgICAgICAgICAgIGlmIGtleSBub3QgaW4gc2VlbjoKICAgICAgICAgICAgICAgIHNlZW4uYWRkKGtleSkKICAgICAgICAgICAgICAgIGNodW5rcy5hcHBlbmQoY2h1bmspCgogICAgcmV0dXJuIGNodW5rcwoKCmRlZiBfZGlyZWN0b3JfY2h1bmtzKHBhZ2UsIG1hbmFnZXJfbmFtZSk6CiAgICAiIiLopIfmlbDljLvluKvjg5rjg7zjgrjjgafjgoLjgIHnj77lnKjjga7nrqHnkIbogIXmnKzkurrjga7ov5Hlgo3jgaDjgZHjgpLntYzmrbTlgJnoo5zjgavjgZnjgovjgIIiIiIKICAgIG5hbWU9bm9ybWFsaXplX3BlcnNvbihtYW5hZ2VyX25hbWUpCiAgICBpZiBub3QgbmFtZToKICAgICAgICByZXR1cm4gW10KICAgIHNvdXA9QmVhdXRpZnVsU291cChwYWdlLmh0bWwsImh0bWwucGFyc2VyIikKICAgIGZvciB4IGluIHNvdXAuc2VsZWN0KCJzY3JpcHQsc3R5bGUsbm9zY3JpcHQsdGVtcGxhdGUiKToKICAgICAgICB4LmRlY29tcG9zZSgpCgogICAgIyB2MjUuMzog5Y+k44GE5Yy76ZmiSFDjgafjga/lvbnogbfjg7vljLvluKvlkI3jgYznlLvlg4/jga4gYWx0L3RpdGxlIOOBq+OBl+OBi+WFpeOBo+OBpuOBhOOBquOBhOOBk+OBqOOBjOOBguOCi+OAggogICAgIyDjgZ3jga7loLTlkIjjgoIgRE9NIOimi+WHuuOBl+OBqOOBl+OBpuaJseOBiOOCi+OCiOOBhuOAgeeUu+WDj+OBruS7o+abv+ODhuOCreOCueODiOOCkuWPr+imluaWh+Wtl+WIl+OBuOWxlemWi+OBmeOCi+OAggogICAgZm9yIGltZyBpbiBzb3VwLmZpbmRfYWxsKCJpbWciKToKICAgICAgICBhbHQ9X2NsZWFuKGltZy5nZXQoImFsdCIpIG9yIGltZy5nZXQoInRpdGxlIikgb3IgIiIpLnN0cmlwKCkKICAgICAgICBpZiBhbHQ6CiAgICAgICAgICAgIGltZy5yZXBsYWNlX3dpdGgoIiAiICsgYWx0ICsgIiAiKQoKICAgICMgdjI1LjI6IOimi+WHuuOBl+OBp+mZoumVt+acrOS6uuOBjOaYjuekuuOBleOCjOOBpuOBhOOCi+ODmuODvOOCuOOBr+OAgeOBvuOBmuimi+WHuuOBl+Wig+eVjOOBp+S6uueJqeWIhumbouOBmeOCi+OAggogICAgaGVhZGluZ19jaHVua3M9X21hbmFnZXJfaGVhZGluZ19jaHVua3Moc291cCxtYW5hZ2VyX25hbWUpCiAgICBpZiBoZWFkaW5nX2NodW5rczoKICAgICAgICByZXR1cm4gaGVhZGluZ19jaHVua3MKCiAgICBjaHVua3M9W10KICAgIHNlZW49c2V0KCkKICAgIGZvciBub2RlIGluIHNvdXAuZmluZF9hbGwoc3RyaW5nPVRydWUpOgogICAgICAgIHJhdz1fY2xlYW4obm9kZSkKICAgICAgICBpZiBuYW1lIG5vdCBpbiBub3JtYWxpemVfcGVyc29uKHJhdyk6CiAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgY3VyPW5vZGUucGFyZW50CiAgICAgICAgc2VsZWN0ZWQ9Tm9uZQogICAgICAgICMgdjI1OiA1MDAw5paH5a2X57Sa44Gu6Kaq6KaB57Sg44G+44Gn5bqD44GS44Ga44CB5pys5Lq644Gu6L+R5YKN44KS5YSq5YWI44GZ44KL44CCCiAgICAgICAgZm9yIF8gaW4gcmFuZ2UoNyk6CiAgICAgICAgICAgIGlmIGN1ciBpcyBOb25lOgogICAgICAgICAgICAgICAgYnJlYWsKICAgICAgICAgICAgdGV4dD1fY2xlYW4oY3VyLmdldF90ZXh0KCIgIixzdHJpcD1UcnVlKSkKICAgICAgICAgICAgaWYgbmFtZSBpbiBub3JtYWxpemVfcGVyc29uKHRleHQpIGFuZCByZS5zZWFyY2gociLpmaLplbd8566h55CG6ICFfOe1jOattHznlaXmrbR844OX44Ot44OV44Kj44O844OrfOWNkualrXzljLvluKvlhY3oqLF85Yy757GNfOWbveWutuippumokyIsdGV4dCk6CiAgICAgICAgICAgICAgICB0cmltbWVkPV90cmltX3RvX21hbmFnZXJfYmxvY2sodGV4dCxtYW5hZ2VyX25hbWUpCiAgICAgICAgICAgICAgICBpZiB0cmltbWVkOgogICAgICAgICAgICAgICAgICAgIHNlbGVjdGVkPXRyaW1tZWQKICAgICAgICAgICAgICAgIGlmIHNlbGVjdGVkIGFuZCBsZW4oc2VsZWN0ZWQpPD0xODAwIGFuZCByZS5zZWFyY2gociLljZLmpa185Yy75bir5YWN6KixfOWMu+exjXzlm73lrrboqabpqJMiLHNlbGVjdGVkKToKICAgICAgICAgICAgICAgICAgICBicmVhawogICAgICAgICAgICBjdXI9Y3VyLnBhcmVudAogICAgICAgIGlmIHNlbGVjdGVkOgogICAgICAgICAgICBrZXk9c2VsZWN0ZWRbOjEyMDBdCiAgICAgICAgICAgIGlmIGtleSBub3QgaW4gc2VlbjoKICAgICAgICAgICAgICAgIHNlZW4uYWRkKGtleSkKICAgICAgICAgICAgICAgIGNodW5rcy5hcHBlbmQoc2VsZWN0ZWQpCgogICAgIyBET03kuIrjgaflkI3liY3jgYzliIblibLjgZXjgozjgovloLTlkIjjgoLjgIHjg5rjg7zjgrjlhajkvZPjgafjga/jgarjgY/mnKzkurrlkI3lkajovrrjgaDjgZHjgpLkvb/jgYbjgIIKICAgIGZ1bGw9X2NsZWFuKHNvdXAuZ2V0X3RleHQoIiAiLHN0cmlwPVRydWUpKQogICAgaWYgbm90IGNodW5rcyBhbmQgbmFtZSBpbiBub3JtYWxpemVfcGVyc29uKGZ1bGwpOgogICAgICAgIHRyaW1tZWQ9X3RyaW1fdG9fbWFuYWdlcl9ibG9jayhmdWxsLG1hbmFnZXJfbmFtZSkKICAgICAgICBpZiB0cmltbWVkIGFuZCByZS5zZWFyY2gociLpmaLplbd8566h55CG6ICFfOe1jOattHznlaXmrbR844OX44Ot44OV44Kj44O844OrfOWNkualrXzljLvluKvlhY3oqLF85Yy757GNfOWbveWutuippumokyIsdHJpbW1lZCk6CiAgICAgICAgICAgIGNodW5rcz1bdHJpbW1lZF0KICAgIHJldHVybiBjaHVua3MKCgpkZWYgX3llYXJfbWF0Y2hlcyh0ZXh0KToKICAgIHJldHVybiBbKG0uc3RhcnQoKSxtLmVuZCgpLHBhcnNlX3llYXIobS5ncm91cCgwKSksbS5ncm91cCgwKSkgZm9yIG0gaW4gcmUuZmluZGl0ZXIoWUVBUl9QQVRURVJOLHRleHQpXQoKCmRlZiBfbmVhcmVzdF95ZWFyKHRleHQsIGtleXdvcmRfbWF0Y2gsIGJlZm9yZT01NSwgYWZ0ZXI9MTQpOgogICAgIiIi5Y2S5qWtL+WMu+exjeOCreODvOODr+ODvOODieOBq+WvvuW/nOOBmeOCi+W5tOOCkuaOoeeUqOOBmeOCi+OAggoKICAgIHYyNS4xOiDjgIwxOTYw5bm0IOWMu+WtpumDqOWNkualreOAgjE5NjPlubTjgavmuKHnsbPjgI3jga7jgojjgYbjgarmlofjgafjga/jgIEKICAgIOWNkualreW+jOOBq+ePvuOCjOOCi+WLpOWLmeODu+eVmeWtpuW5tOOBp+OBr+OBquOBj+OAgeOCreODvOODr+ODvOODieebtOWJjeOBruW5tOOCkuW/heOBmuWEquWFiOOBmeOCi+OAggogICAg55u05YmN44Gr5bm044GM54Sh44GE6KGo6KiY77yI44CM5Yy75a2m6YOo5Y2S5qWtIDE5OTTlubTjgI3nrYnvvInjga7jgb/lvozmlrnlubTjgbjjg5Xjgqnjg7zjg6vjg5Djg4Pjgq/jgZnjgovjgIIKICAgICIiIgogICAgeWVhcnM9X3llYXJfbWF0Y2hlcyh0ZXh0KQogICAgYmVmb3JlX2NhbmRpZGF0ZXM9W10KICAgIGFmdGVyX2NhbmRpZGF0ZXM9W10KICAgIGZvciB5cyx5ZSx5ZWFyLHJhdyBpbiB5ZWFyczoKICAgICAgICBpZiBub3QgeWVhciBvciBub3QgKDE5MDA8PXllYXI8PXRvZGF5X2phcGFuKCkueWVhcik6CiAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgaWYgeWU8PWtleXdvcmRfbWF0Y2guc3RhcnQoKToKICAgICAgICAgICAgZGlzdGFuY2U9a2V5d29yZF9tYXRjaC5zdGFydCgpLXllCiAgICAgICAgICAgIGlmIGRpc3RhbmNlPD1iZWZvcmU6CiAgICAgICAgICAgICAgICBiZWZvcmVfY2FuZGlkYXRlcy5hcHBlbmQoKGRpc3RhbmNlLHllYXIscmF3LHlzLHllKSkKICAgICAgICBlbGlmIHlzPj1rZXl3b3JkX21hdGNoLmVuZCgpOgogICAgICAgICAgICBkaXN0YW5jZT15cy1rZXl3b3JkX21hdGNoLmVuZCgpCiAgICAgICAgICAgIGlmIGRpc3RhbmNlPD1hZnRlcjoKICAgICAgICAgICAgICAgIGFmdGVyX2NhbmRpZGF0ZXMuYXBwZW5kKChkaXN0YW5jZSx5ZWFyLHJhdyx5cyx5ZSkpCiAgICBpZiBiZWZvcmVfY2FuZGlkYXRlczoKICAgICAgICByZXR1cm4gbWluKGJlZm9yZV9jYW5kaWRhdGVzLGtleT1sYW1iZGEgeDp4WzBdKQogICAgaWYgYWZ0ZXJfY2FuZGlkYXRlczoKICAgICAgICByZXR1cm4gbWluKGFmdGVyX2NhbmRpZGF0ZXMsa2V5PWxhbWJkYSB4OnhbMF0pCiAgICByZXR1cm4gTm9uZQoKCmRlZiBfeWVhcnNfZnJvbV9jaHVua3MoY2h1bmtzLCBraW5kKToKICAgIHJlc3VsdHM9W10KICAgIHNlZW49c2V0KCkKICAgIGlmIGtpbmQ9PSJncmFkdWF0aW9uIjoKICAgICAgICBrZXl3b3JkPXJlLmNvbXBpbGUoCiAgICAgICAgICAgIHIiKD865Yy75a2m6YOofOWMu+WtpuenkXzljLvnp5HlpKflraYpW17jgILjgIHvvJs7XG5dezAsMTh9Pyg/OuWNkualrXzljZIpIixyZS5JCiAgICAgICAgKQogICAgZWxzZToKICAgICAgICBrZXl3b3JkPXJlLmNvbXBpbGUociLljLvluKvlhY3oqLEoPzrlj5blvpcpP3zljLvnsY0oPzrnmbvpjLIpP3zljLvluKvlm73lrrboqabpqJMoPzrlkIjmoLwpPyIscmUuSSkKCiAgICBmb3IgdGV4dCBpbiBjaHVua3M6CiAgICAgICAgZm9yIGttIGluIGtleXdvcmQuZmluZGl0ZXIodGV4dCk6CiAgICAgICAgICAgIG5lYXJlc3Q9X25lYXJlc3RfeWVhcih0ZXh0LGttLGJlZm9yZT02MCBpZiBraW5kPT0iZ3JhZHVhdGlvbiIgZWxzZSA0NSxhZnRlcj0xNCkKICAgICAgICAgICAgaWYgbm90IG5lYXJlc3Q6CiAgICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBfLHllYXIsXyx5cyx5ZT1uZWFyZXN0CiAgICAgICAgICAgIGxlZnQ9bWF4KDAsbWluKHlzLGttLnN0YXJ0KCkpLTM1KQogICAgICAgICAgICByaWdodD1taW4obGVuKHRleHQpLG1heCh5ZSxrbS5lbmQoKSkrNTUpCiAgICAgICAgICAgIGV2aWRlbmNlPXJlLnN1YihyIlxzKyIsIiAiLHRleHRbbGVmdDpyaWdodF0pLnN0cmlwKCkKICAgICAgICAgICAga2V5PSh5ZWFyLGV2aWRlbmNlKQogICAgICAgICAgICBpZiBrZXkgaW4gc2VlbjoKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIHNlZW4uYWRkKGtleSkKICAgICAgICAgICAgcmVzdWx0cy5hcHBlbmQoKHllYXIsZXZpZGVuY2UpKQogICAgcmV0dXJuIHJlc3VsdHMKCmRlZiBncmFkdWF0aW9uX2V2aWRlbmNlKHJlY29yZCxwYWdlcyk6CiAgICAiIiLnj77lnKjjga7pmaLplbcv566h55CG6ICF5pys5Lq644Gu6L+R5YKN44GL44KJ5aSn5a2m5Yy75a2m6YOo44Gu5Y2S5qWt5bm044KS5oq95Ye644CC6KSH5pWw5Yy75bir44Oa44O844K444Gr44KC5a++5b+c44CCIiIiCiAgICBtYW5hZ2VyPXJlY29yZC5nZXQoIm1hbmFnZXJfbmFtZSIsIiIpCiAgICByZXN1bHQ9W10KICAgIHNlZW49c2V0KCkKICAgIGZvciBwYWdlIGluIHBhZ2VzOgogICAgICAgIGZvciB5ZWFyLGV2aWRlbmNlIGluIF95ZWFyc19mcm9tX2NodW5rcyhfZGlyZWN0b3JfY2h1bmtzKHBhZ2UsbWFuYWdlciksImdyYWR1YXRpb24iKToKICAgICAgICAgICAga2V5PSh5ZWFyLHBhZ2UudXJsKQogICAgICAgICAgICBpZiBrZXkgaW4gc2VlbjoKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIHNlZW4uYWRkKGtleSkKICAgICAgICAgICAgcmVzdWx0LmFwcGVuZCh7InllYXIiOnllYXIsInVybCI6cGFnZS51cmwsImV2aWRlbmNlIjpldmlkZW5jZVs6MzAwXSwiZG9jdG9yX25hbWUiOm1hbmFnZXJ9KQogICAgcmV0dXJuIHJlc3VsdAoKCmRlZiBsaWNlbnNlX2V2aWRlbmNlKHJlY29yZCxwYWdlcyk6CiAgICBtYW5hZ2VyPXJlY29yZC5nZXQoIm1hbmFnZXJfbmFtZSIsIiIpCiAgICByZXN1bHQ9W10KICAgIHNlZW49c2V0KCkKICAgIGZvciBwYWdlIGluIHBhZ2VzOgogICAgICAgIGZvciB5ZWFyLGV2aWRlbmNlIGluIF95ZWFyc19mcm9tX2NodW5rcyhfZGlyZWN0b3JfY2h1bmtzKHBhZ2UsbWFuYWdlciksImxpY2Vuc2UiKToKICAgICAgICAgICAga2V5PSh5ZWFyLHBhZ2UudXJsKQogICAgICAgICAgICBpZiBrZXkgaW4gc2VlbjoKICAgICAgICAgICAgICAgIGNvbnRpbnVlCiAgICAgICAgICAgIHNlZW4uYWRkKGtleSkKICAgICAgICAgICAgcmVzdWx0LmFwcGVuZCh7InllYXIiOnllYXIsInVybCI6cGFnZS51cmwsImV2aWRlbmNlIjpldmlkZW5jZVs6MzAwXSwiZG9jdG9yX25hbWUiOm1hbmFnZXJ9KQogICAgcmV0dXJuIHJlc3VsdAoKCmRlZiBfaW50X2FnZSh2YWx1ZSk6CiAgICB0cnk6CiAgICAgICAgYWdlPWludChmbG9hdChzdHIodmFsdWUpLnN0cmlwKCkpKQogICAgICAgIHJldHVybiBhZ2UgaWYgMjA8PWFnZTw9MTAwIGVsc2UgTm9uZQogICAgZXhjZXB0IChUeXBlRXJyb3IsVmFsdWVFcnJvcik6CiAgICAgICAgcmV0dXJuIE5vbmUKCgpkZWYgZXN0aW1hdGVfcHJvZmlsZV9hZ2UocmVjb3JkLHBhZ2VzPSgpLGN1cnJlbnRfeWVhcj1Ob25lKToKICAgICIiIuW5tOm9ouagueaLoOOBruWEquWFiOmghjog5YWs55qEL+WPlui+vOa4iOOBv+W5tOm9ouODu+eUn+W5tOODu+WMu+exjeW5tCDihpIgSFDjga7ljLvnsY3lubQg4oaSIEhQ5Y2S5qWt5bm044CCCgogICAg5oyH5a6a5bm05pyI5pel44Gv6ZaL5qWt5pmC5pyf44Gu5YWs55qE5oOF5aCx44Go44GX44Gm5YWI44Gr56K66KqN44GZ44KL44GM44CB5bm06b2i44Gd44Gu44KC44Gu44Gv566X5Ye644Gn44GN44Gq44GE44Gf44KBCiAgICDlubTpvaLlgKTjga7moLnmi6Djgavjga/kvb/jgo/jgZrjgIHntYzmrbTjgbjoh6rli5Xjg5Xjgqnjg7zjg6vjg5Djg4Pjgq/jgZnjgovjgIIKICAgICIiIgogICAgY3VycmVudF95ZWFyPWN1cnJlbnRfeWVhciBvciB0b2RheV9qYXBhbigpLnllYXIKICAgIGRlc2lnbmF0aW9uPXBhcnNlX2RhdGUocmVjb3JkLmdldCgiZGVzaWduYXRpb25fZGF0ZSIsIiIpKQogICAgbWFzdGVyX2NvbnRleHQ9ewogICAgICAgICJkZXNpZ25hdGlvbl9kYXRlIjpkZXNpZ25hdGlvbi5pc29mb3JtYXQoKSBpZiBkZXNpZ25hdGlvbiBlbHNlIHN0cihyZWNvcmQuZ2V0KCJkZXNpZ25hdGlvbl9kYXRlIiwiIikgb3IgIiIpLAogICAgICAgICJyZWdpc3RyYXRpb25fcmVhc29uIjpzdHIocmVjb3JkLmdldCgicmVnaXN0cmF0aW9uX3JlYXNvbiIsIiIpIG9yICIiKSwKICAgICAgICAib3duZXJfbWFuYWdlcl9lcXVhbCI6cmVjb3JkLmdldCgib3duZXJfbWFuYWdlcl9lcXVhbCIpLAogICAgfQoKICAgIHJlc3VsdD17CiAgICAgICAgImRvY3Rvcl9uYW1lIjpyZWNvcmQuZ2V0KCJtYW5hZ2VyX25hbWUiLCIiKSwibGljZW5zZV9yZWdpc3RyYXRpb25feWVhciI6Tm9uZSwiZ3JhZHVhdGlvbl95ZWFyIjpOb25lLAogICAgICAgICJncmFkdWF0aW9uX2V2aWRlbmNlIjpbXSwibGljZW5zZV9ldmlkZW5jZSI6W10sImFnZV9wcm9iYWJpbGl0eV91bmRlcl81OSI6Tm9uZSwKICAgICAgICAiYWdlX2VzdGltYXRpb25fc291cmNlIjoiVU5LTk9XTiIsImFnZV9lc3RpbWF0aW9uX2NvbmZpZGVuY2UiOiJVTktOT1dOIiwiYWdlX2VzdGltYXRpb25feWVhciI6Y3VycmVudF95ZWFyLAogICAgICAgICJhZ2VfbWFzdGVyX2NvbnRleHQiOm1hc3Rlcl9jb250ZXh0LAogICAgfQoKICAgIGRpcmVjdF9hZ2U9bmV4dCgoX2ludF9hZ2UocmVjb3JkLmdldChrKSkgZm9yIGsgaW4gWyJtYW5hZ2VyX2FnZSIsImRvY3Rvcl9hZ2UiXSBpZiBfaW50X2FnZShyZWNvcmQuZ2V0KGspKSBpcyBub3QgTm9uZSksTm9uZSkKICAgIGlmIGRpcmVjdF9hZ2UgaXMgTm9uZSBhbmQgcmVjb3JkLmdldCgib3duZXJfbWFuYWdlcl9lcXVhbCIpIGlzIFRydWU6CiAgICAgICAgZGlyZWN0X2FnZT1faW50X2FnZShyZWNvcmQuZ2V0KCJvd25lcl9hZ2UiKSkKICAgIGlmIGRpcmVjdF9hZ2UgaXMgbm90IE5vbmU6CiAgICAgICAgcmV0dXJuIHsqKnJlc3VsdCwiYWdlX3Byb2JhYmlsaXR5X3VuZGVyXzU5IjoxLjAgaWYgZGlyZWN0X2FnZTw9NTkgZWxzZSAwLjAsCiAgICAgICAgICAgICAgICAiYWdlX2VzdGltYXRpb25fc291cmNlIjoi5Y6a55Sf5bGAL+ijnOi2s+ODnuOCueOCv+OBrueuoeeQhuiAheW5tOm9oiIsImFnZV9lc3RpbWF0aW9uX2NvbmZpZGVuY2UiOiJPRkZJQ0lBTF9WQUxVRSIsCiAgICAgICAgICAgICAgICAiYWdlX2VzdGltYXRpb25fcmVhc29uIjpmIuWFrOeahOODu+WPlui+vOa4iOOBv+euoeeQhuiAheW5tOm9oiB7ZGlyZWN0X2FnZX3mrbPjgpLkvb/nlKjjgILmjIflrprlubTmnIjml6XjgoLnorroqo3muIjjgb/jgIIifQoKICAgIGJpcnRoPXBhcnNlX3llYXIocmVjb3JkLmdldCgibWFuYWdlcl9iaXJ0aF95ZWFyIikgb3IgcmVjb3JkLmdldCgiZG9jdG9yX2JpcnRoX3llYXIiKSkKICAgIGlmIGJpcnRoIGFuZCAxOTAwPD1iaXJ0aDw9Y3VycmVudF95ZWFyOgogICAgICAgIGFwcHJveD1jdXJyZW50X3llYXItYmlydGgKICAgICAgICBwcm9iYWJpbGl0eT0xLjAgaWYgYXBwcm94PD01OCBlbHNlIC41IGlmIGFwcHJveD09NTkgZWxzZSAwLjAKICAgICAgICByZXR1cm4geyoqcmVzdWx0LCJhZ2VfcHJvYmFiaWxpdHlfdW5kZXJfNTkiOnByb2JhYmlsaXR5LCJhZ2VfZXN0aW1hdGlvbl9zb3VyY2UiOiLljprnlJ/lsYAv6KOc6Laz44Oe44K544K/44Gu566h55CG6ICF55Sf5bm0IiwKICAgICAgICAgICAgICAgICJhZ2VfZXN0aW1hdGlvbl9jb25maWRlbmNlIjoiT0ZGSUNJQUxfWUVBUiIsImFnZV9lc3RpbWF0aW9uX3JlYXNvbiI6IueUn+W5tOOBruOBv+OBruOBn+OCgeiqleeUn+aXpeacquWPjeaYoOOAguaMh+WumuW5tOaciOaXpeOCgueiuuiqjea4iOOBv+OAgiJ9CgogICAgZ3JhZF9ldmlkZW5jZT1ncmFkdWF0aW9uX2V2aWRlbmNlKHJlY29yZCxwYWdlcykgaWYgcGFnZXMgZWxzZSByZWNvcmQuZ2V0KCJncmFkdWF0aW9uX2V2aWRlbmNlIixbXSkgb3IgW10KICAgIGxpY19ldmlkZW5jZT1saWNlbnNlX2V2aWRlbmNlKHJlY29yZCxwYWdlcykgaWYgcGFnZXMgZWxzZSByZWNvcmQuZ2V0KCJsaWNlbnNlX2V2aWRlbmNlIixbXSkgb3IgW10KICAgIGdyYWRfeWVhcnM9e2VbInllYXIiXSBmb3IgZSBpbiBncmFkX2V2aWRlbmNlIGlmIGUuZ2V0KCJ5ZWFyIil9CiAgICBsaWNfeWVhcnM9e2VbInllYXIiXSBmb3IgZSBpbiBsaWNfZXZpZGVuY2UgaWYgZS5nZXQoInllYXIiKX0KCiAgICAjIHYyNS40OiDlvLfliLblho3oqr/mn7vjgafjga/jgIHpgY7ljrvjga7oh6rli5Xoqr/mn7vntZDmnpzjgojjgorku4rlm57jga5IUOWun+a4rOagueaLoOOCkuWEquWFiOOBmeOCi+OAggogICAgIyBzdG9yZS5nZXQoKSDjga/liY3lm57jga4gcmVzZWFyY2hfcmVzdWx0cyDjgoIgcmVjb3JkIOOBq+WQq+OCgeOCi+OBn+OCgeOAgeWPpOOBhOiqpOWIpOWumuW5tOOCkuOBneOBruOBvuOBvgogICAgIyByZWNvcmRbImdyYWR1YXRpb25feWVhciJdIOOBi+OCieWGjeWIqeeUqOOBmeOCi+OBqOOAgeS7iuWbnuOBruato+OBl+OBhCBldmlkZW5jZSDjgpLkuIrmm7jjgY3jgZfjgabjgZfjgb7jgYbjgIIKICAgICMg44Gf44Gg44GX5omL5YuV5L+u5q2j5YCk44Gv5pyA5YSq5YWI44Gn57at5oyB44GZ44KL44CCCiAgICBtYW51YWxfZmllbGRzPXNldChyZWNvcmQuZ2V0KCJtYW51YWxfZmllbGRzIikgb3IgW10pCgogICAgcmVjb3JkX2xpYz1wYXJzZV95ZWFyKHJlY29yZC5nZXQoImxpY2Vuc2VfcmVnaXN0cmF0aW9uX3llYXIiKSkKICAgIGlmICJsaWNlbnNlX3JlZ2lzdHJhdGlvbl95ZWFyIiBpbiBtYW51YWxfZmllbGRzIGFuZCByZWNvcmRfbGljOgogICAgICAgIGxpYz1yZWNvcmRfbGljCiAgICBlbGlmIGxlbihsaWNfeWVhcnMpPT0xOgogICAgICAgIGxpYz1uZXh0KGl0ZXIobGljX3llYXJzKSkKICAgIGVsaWYgbm90IGxpY195ZWFyczoKICAgICAgICBsaWM9cmVjb3JkX2xpYwogICAgZWxzZToKICAgICAgICBsaWM9Tm9uZQogICAgaWYgbGljIGFuZCBub3QgMTkwMDw9bGljPD1jdXJyZW50X3llYXI6CiAgICAgICAgbGljPU5vbmUKCiAgICByZWNvcmRfZ3JhZD1wYXJzZV95ZWFyKHJlY29yZC5nZXQoImdyYWR1YXRpb25feWVhciIpKQogICAgaWYgImdyYWR1YXRpb25feWVhciIgaW4gbWFudWFsX2ZpZWxkcyBhbmQgcmVjb3JkX2dyYWQ6CiAgICAgICAgZ3JhZD1yZWNvcmRfZ3JhZAogICAgZWxpZiBsZW4oZ3JhZF95ZWFycyk9PTE6CiAgICAgICAgZ3JhZD1uZXh0KGl0ZXIoZ3JhZF95ZWFycykpCiAgICBlbGlmIG5vdCBncmFkX3llYXJzOgogICAgICAgIGdyYWQ9cmVjb3JkX2dyYWQKICAgIGVsc2U6CiAgICAgICAgZ3JhZD1Ob25lCiAgICBpZiBncmFkIGFuZCBub3QgMTkwMDw9Z3JhZDw9Y3VycmVudF95ZWFyOgogICAgICAgIGdyYWQ9Tm9uZQoKICAgIHJlc3VsdC51cGRhdGUobGljZW5zZV9yZWdpc3RyYXRpb25feWVhcj1saWMsZ3JhZHVhdGlvbl95ZWFyPWdyYWQsCiAgICAgICAgICAgICAgICAgIGdyYWR1YXRpb25fZXZpZGVuY2U9Z3JhZF9ldmlkZW5jZSxsaWNlbnNlX2V2aWRlbmNlPWxpY19ldmlkZW5jZSkKCiAgICBpZiBsZW4oZ3JhZF95ZWFycyk+MSBvciBsZW4obGljX3llYXJzKT4xIG9yIChsaWMgYW5kIGdyYWQgYW5kIChsaWM8Z3JhZCBvciBsaWMtZ3JhZD40KSk6CiAgICAgICAgcmV0dXJuIHsqKnJlc3VsdCwiYWdlX2VzdGltYXRpb25fY29uZmlkZW5jZSI6IlJFVklFVyIsCiAgICAgICAgICAgICAgICAiYWdlX2VzdGltYXRpb25fcmVhc29uIjoi6Zmi6ZW35pys5Lq644Gu5Y2S5qWt5bm0L+WMu+exjeW5tOWAmeijnOOBjOikh+aVsOOBvuOBn+OBr+efm+ebvuOBl+OBpuOBhOOBvuOBmeOAguaMh+WumuW5tOaciOaXpeOBp+OBr+W5tOm9ouOCkueiuuWumuOBp+OBjeOBquOBhOOBn+OCgeimgeeiuuiqjeOAgiJ9CgogICAgaWYgbm90IGxpYyBhbmQgbm90IGdyYWQ6CiAgICAgICAgc291cmNlPShmIuWOmueUn+WxgOaMh+WumuW5tOaciOaXpSB7ZGVzaWduYXRpb24uaXNvZm9ybWF0KCl9IOOCkueiuuiqje+8iOW5tOm9ouOBr+ebtOaOpeeul+WHuuS4jeWPr++8iSIgaWYgZGVzaWduYXRpb24gZWxzZSAi5bm06b2i5qC55oug5pyq5Y+W5b6XIikKICAgICAgICByZXR1cm4geyoqcmVzdWx0LCJhZ2VfZXN0aW1hdGlvbl9zb3VyY2UiOnNvdXJjZSwKICAgICAgICAgICAgICAgICJhZ2VfZXN0aW1hdGlvbl9yZWFzb24iOiLmjIflrprlubTmnIjml6Xjga/plovmpa3mmYLmnJ/jga7moLnmi6DjgafjgYLjgorpmaLplbflubTpvaLjgafjga/jgarjgYTjgZ/jgoHjgIFIUOOBrumZoumVt+e1jOattOOBvuOBp+eiuuiqjeOBl+OBn+OBjOWNkualreW5tC/ljLvnsY3lubTjgpLlj5blvpfjgafjgY3jgb7jgZvjgpPjgafjgZfjgZ/jgIIifQoKICAgIGlmIGxpYzoKICAgICAgICBlc3RpbWF0ZT1BZ2VFc3RpbWF0b3IoKS5lc3RpbWF0ZShsaWMsY3VycmVudF95ZWFyKQogICAgICAgIGlmIHJlY29yZC5nZXQoImxpY2Vuc2VfcmVnaXN0cmF0aW9uX3llYXIiKToKICAgICAgICAgICAgc291cmNlPXJlY29yZC5nZXQoImxpY2Vuc2Vfc291cmNlIikgb3IgIuWOmueUn+WxgC/lj5bovrzmuIjjgb/ljLvnsY3nmbvpjLLlubQiCiAgICAgICAgZWxzZToKICAgICAgICAgICAgc291cmNlPWxpY19ldmlkZW5jZVswXVsidXJsIl0gaWYgbGljX2V2aWRlbmNlIGVsc2UgIkhQ6Zmi6ZW357WM5q2044Gu5Yy75bir5YWN6Kix5bm0IgogICAgZWxzZToKICAgICAgICBjb25maWc9ZGVlcGNvcHkoQWdlRXN0aW1hdG9yKCkuY29uZmlnKQogICAgICAgIGNvbmZpZ1sibmF0aW9uYWxfZXhhbV9kZWxheV9kaXN0cmlidXRpb24iXT17ImRlbGF5XzAiOjEuMH0KICAgICAgICBlc3RpbWF0ZT1BZ2VFc3RpbWF0b3IoY29uZmlnKS5lc3RpbWF0ZShncmFkLGN1cnJlbnRfeWVhcikKICAgICAgICBzb3VyY2U9Z3JhZF9ldmlkZW5jZVswXVsidXJsIl0gaWYgZ3JhZF9ldmlkZW5jZSBlbHNlICJIUOmZoumVt+e1jOattOOBruWkp+WtpuWNkualreW5tCIKCiAgICByZXR1cm4geyoqcmVzdWx0LCJhZ2VfcHJvYmFiaWxpdHlfdW5kZXJfNTkiOmVzdGltYXRlLnByb2JhYmlsaXR5LCJhZ2VfZXN0aW1hdGlvbl9zb3VyY2UiOnNvdXJjZSwKICAgICAgICAgICAgImFnZV9lc3RpbWF0aW9uX2NvbmZpZGVuY2UiOiJNT0RFTF9FU1RJTUFURSIsCiAgICAgICAgICAgICJhZ2VfZXN0aW1hdGlvbl9yZWFzb24iOiLmjIflrprlubTmnIjml6XjgpLlhYjjgavnorroqo3jgZfjgIHlubTpvaLjgpLnm7TmjqXnrpflh7rjgafjgY3jgarjgYTjgZ/jgoHpmaLplbfmnKzkurrjga7ljLvnsY3lubQv5aSn5a2m5Y2S5qWt5bm044GL44KJ5Luu5a6a5YiG5biD44GnNTnmrbPku6XkuIvnorrnjofjgpLmjqjlrprjgIIifQo='))
    py_compile.compile(str(PROFILE), doraise=True)
    (ROOT / "VERSION_RESEARCH").write_text("2.5.4-research\n", encoding="utf-8")
except Exception as exc:
    shutil.copy2(backup / "profiles.py", PROFILE)
    print("適用失敗。元に戻しました。")
    print("エラー:", repr(exc))
    raise SystemExit(1)

print("v25.4 を適用しました。")
print("【重要修正】")
print("・再調査時は、前回の自動調査で保存された古い卒業年より、今回HPから再取得した卒業年根拠を優先します。")
print("・手動修正された卒業年/医籍年は従来どおり最優先です。")
print("・v25.3の人物分離、v25.1の集客施策/内視鏡ガード等は維持します。")
print("・SQLite / UUID / Maps / Comdesk元データは変更していません。")
print("バックアップ:", backup.name)

print("\n=== v25.4 合成テスト ===")
from src.enrichment.hp_analysis import Page
from src.enrichment.profiles import estimate_profile_age

record = {
    "manager_name": "掛谷 和俊",
    "designation_date": "2004-10-01",
    "registration_reason": "組織変更",
    "owner_manager_equal": True,
    "graduation_year": 1963,  # 前回誤判定が残っている状態を再現
    "manual_fields": [],
}
html = """
<html><body>
<h3><img alt="院長 掛谷和俊"></h3>
<p>1982年宮崎大学医学部卒業。消化器癌の研究で博士号を取得。</p>
<h3><img alt="特別顧問 新谷弘実"></h3>
<p>1960年順天堂大学医学部卒業。1963年に渡米。</p>
</body></html>
"""
test = estimate_profile_age(record, [Page("https://example.com/doctor.html", html)], current_year=2026)
print("fresh_graduation_year =", test.get("graduation_year"))
if test.get("graduation_year") != 1982:
    print("合成テスト失敗。実データ再調査は行いません。")
    raise SystemExit(1)

record["manual_fields"] = ["graduation_year"]
manual_test = estimate_profile_age(record, [Page("https://example.com/doctor.html", html)], current_year=2026)
print("manual_override_year =", manual_test.get("graduation_year"))
if manual_test.get("graduation_year") != 1963:
    print("手動値優先テスト失敗。実データ再調査は行いません。")
    raise SystemExit(1)

print("synthetic_stale_result_override = PASS")
print("synthetic_manual_override = PASS")

from src.master.store import ClinicStore, now, dumps
from src.master.jobs import run_job, job_status
from src.enrichment.search_provider import TavilySearchProvider

store = ClinicStore(DB)
provider = TavilySearchProvider("")

def run_ids(ids, label):
    jid = uuid.uuid4().hex
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            "INSERT INTO research_jobs(id,kind,options_json,max_searches,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (jid, "hp", dumps({"force": True, "max_pages": 20}), 0, now(), now())
        )
        c.executemany(
            "INSERT INTO research_job_items(job_id,clinic_id) VALUES(?,?)",
            [(jid, cid) for cid in ids]
        )

    print("\n" + label)
    print("ジョブ:", jid)
    print("対象:", len(ids), "件")
    holder = {"ok": None, "error": None}

    def worker():
        try:
            holder["ok"] = run_job(store, jid, provider)
        except Exception as exc:
            holder["error"] = repr(exc)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    last = None
    while th.is_alive():
        try:
            s = job_status(store, jid)
            done = s["counts"].get("DONE", 0)
            snapshot = (done, s["status"])
            if snapshot != last:
                print(f"進捗 {done}/{s['total']}件 | 状態={s['status']}")
                last = snapshot
        except Exception:
            pass
        time.sleep(2)
    th.join()

    if holder["error"]:
        print("再調査例外:", holder["error"])
        raise SystemExit(1)
    if holder["ok"] is False:
        print("別の調査が実行中です。アプリを停止して再実行してください。")
        raise SystemExit(1)

    s = job_status(store, jid)
    print("完了:", s["status"])
    print("結果:", s["results"])
    print("Tavily検索:", s.get("search_count", 0), "回")
    return jid

# まず半蔵門胃腸クリニックだけ再調査
with store.connect() as c:
    row = c.execute(
        "SELECT id, clinic_name FROM clinics WHERE clinic_name LIKE ? ORDER BY id LIMIT 1",
        ("%半蔵門胃腸クリニック%",)
    ).fetchone()

if not row:
    print("半蔵門胃腸クリニックがDBに見つかりません。")
    raise SystemExit(1)

run_ids([row["id"]], "=== 半蔵門胃腸クリニック実医院テスト ===")

with store.connect() as c:
    rr = c.execute(
        "SELECT result_json FROM research_results WHERE clinic_id=?",
        (row["id"],)
    ).fetchone()

try:
    data = json.loads(rr["result_json"] or "{}") if rr else {}
except Exception:
    data = {}

print("\n=== 実医院の年齢判定 ===")
print("医院:", row["clinic_name"])
print("卒業年:", data.get("graduation_year"))
print("59歳以下確率:", data.get("age_probability_under_59"))
print("年齢信頼度:", data.get("age_estimation_confidence"))
print("卒業年根拠:", data.get("graduation_evidence"))

if data.get("graduation_year") != 1982:
    print("\n実医院テスト: FAIL")
    print("1982年になっていないため、50件再調査は止めました。")
    print("この画面をChatGPTに送ってください。")
    raise SystemExit(2)

print("実医院テスト: PASS（1982年）")

# 最新の50件ジョブと同じ医院を再調査
with store.connect() as c:
    base = c.execute("""
        SELECT j.id
        FROM research_jobs j
        JOIN research_job_items i ON i.job_id=j.id
        WHERE j.kind='hp'
        GROUP BY j.id
        HAVING COUNT(*)=50
        ORDER BY j.rowid DESC
        LIMIT 1
    """).fetchone()
    if not base:
        print("50件の元ジョブが見つかりません。")
        raise SystemExit(1)

    ids = [r[0] for r in c.execute(
        "SELECT clinic_id FROM research_job_items WHERE job_id=? ORDER BY rowid",
        (base["id"],)
    )]

full_job = run_ids(ids, "=== 同じ50医院を v25.4 で再調査 ===")

# 50件CSV出力
OUT = ROOT / "hp_research_results_v254.csv"
con = sqlite3.connect(str(DB))
con.row_factory = sqlite3.Row
rows = con.execute("""
SELECT c.id AS clinic_id,c.clinic_name,c.designation_date,c.maps_website_url,r.result_json
FROM research_job_items i
JOIN clinics c ON c.id=i.clinic_id
LEFT JOIN research_results r ON r.clinic_id=i.clinic_id
WHERE i.job_id=?
ORDER BY i.rowid
""",(full_job,)).fetchall()

def load_json(v):
    try:
        return json.loads(v or "{}")
    except Exception:
        return {}

def join(items):
    return " / ".join(str(x) for x in items if x not in (None, "", []))

out_rows = []
for r in rows:
    d = load_json(r["result_json"])
    signals = [s for s in d.get("marketing_signals", []) or [] if isinstance(s, dict)]
    out_rows.append({
        "clinic_id": r["clinic_id"],
        "調査ロジック": "v25.4",
        "医院名": r["clinic_name"],
        "指定年月日": r["designation_date"],
        "Google Maps HP": r["maps_website_url"],
        "解析HP": d.get("hp_url", ""),
        "research_status": d.get("research_status", ""),
        "hp_status": d.get("hp_status", ""),
        "HPランク": d.get("hp_rank", ""),
        "HPスコア": d.get("hp_score", ""),
        "治療カテゴリ": join(d.get("treatment_categories", []) or []),
        "院長名": d.get("doctor_name", ""),
        "卒業年": d.get("graduation_year", ""),
        "医籍登録年": d.get("license_registration_year", ""),
        "59歳以下確率": f"{d['age_probability_under_59']*100:.1f}%" if isinstance(d.get("age_probability_under_59"), (int,float)) else "",
        "年齢信頼度": d.get("age_estimation_confidence", ""),
        "年齢根拠": d.get("age_estimation_source", ""),
        "集客シグナル数": d.get("marketing_signal_count", len(signals)),
        "集客シグナル": join(s.get("name","") for s in signals),
        "アツさ": d.get("hot_status", ""),
        "HP本人確認理由": join(d.get("hp_match_reason", []) or []),
    })

headers = list(out_rows[0]) if out_rows else []
with OUT.open("w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=headers)
    w.writeheader()
    w.writerows(out_rows)

from collections import Counter
counts = Counter(r.get("research_status","") for r in out_rows)

print("\n=== v25.4 完了 ===")
print("50件集計:", dict(counts))
print("出力CSV:", OUT.resolve())
print("このCSVをChatGPTにアップロードしてください。")
